"""Human-in-the-loop handoff on the SAME live session.

Control-transfer model (one holder at a time, enforced in the app by the bridge):

    agent ──request()──▶ paused ──operator claims──▶ human ──operator resolves──▶ agent
                            │                                      │
                            └──────── timeout / abort ─────────────┴──▶ run ends (failed)

* While `agent` holds control, real keyboard/mouse input is dropped inside the app and an
  on-screen banner says so; the operator cannot fight the automation by accident.
* `paused`: automation has stopped touching the app and raised an intervention request carrying
  the goal/capability, current step, reason, and a redacted screenshot.
* `human`: the operator works in the real application window (same process, same session,
  same signed-on operator). Every click/keystroke is recorded by the bridge.
* On resolve, the recorded actions are attached to the intervention and the run's evidence, control
  returns to `agent`, and the engine re-verifies the current step's checkpoint before continuing:
  it never assumes the human left the app in the expected state.

Transport is a directory of JSON files so the operator console, a CLI, or a pager integration
can all drive it without a broker. Production would use a queue plus a co-browsing view.
"""
from __future__ import annotations

import json
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .evidence import Evidence
from .surface.base import Observation

ROOT = Path(__file__).resolve().parents[1]
INTERVENTIONS = ROOT / "runs" / "interventions"
RESOLVED = {"resume", "approve", "deny", "abort"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class InterventionStore:
    def __init__(self, base: Path = INTERVENTIONS):
        self.base = base
        self.base.mkdir(parents=True, exist_ok=True)

    def _p(self, iid: str) -> Path:
        return self.base / f"{iid}.json"

    def create(self, rec: dict[str, Any]) -> str:
        iid = f"int-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
        rec = {"id": iid, "status": "open", "created_at": _now(), **rec}
        self._p(iid).write_text(json.dumps(rec, indent=2), encoding="utf-8")
        return iid

    def get(self, iid: str) -> dict[str, Any]:
        for _ in range(5):
            try:
                return json.loads(self._p(iid).read_text(encoding="utf-8"))
            except (json.JSONDecodeError, PermissionError):
                time.sleep(0.05)  # writer mid-flight
        return json.loads(self._p(iid).read_text(encoding="utf-8"))

    def update(self, iid: str, **fields: Any) -> dict[str, Any]:
        rec = self.get(iid)
        rec.update(fields)
        tmp = self._p(iid).with_suffix(".tmp")
        tmp.write_text(json.dumps(rec, indent=2), encoding="utf-8")
        tmp.replace(self._p(iid))
        return rec

    def list(self, status: str | None = None) -> list[dict[str, Any]]:
        out = []
        for p in self.base.glob("int-*.json"):
            try:
                rec = json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if status is None or rec.get("status") == status:
                out.append(rec)
        # Newest first, then anything still waiting for a person (open/claimed) floats to the top.
        out.sort(key=lambda r: r.get("created_at", ""), reverse=True)
        out.sort(key=lambda r: 0 if r.get("status") in ("open", "claimed") else 1)
        return out

    # operator-side verbs (used by console and CLI)
    def claim(self, iid: str, operator: str) -> dict[str, Any]:
        rec = self.get(iid)
        if rec["status"] != "open":
            raise ValueError(f"intervention is {rec['status']}, not open")
        return self.update(iid, status="claimed", operator=operator, claimed_at=_now())

    def resolve(self, iid: str, decision: str, notes: str = "") -> dict[str, Any]:
        if decision not in RESOLVED:
            raise ValueError(f"decision must be one of {sorted(RESOLVED)}")
        rec = self.get(iid)
        if rec["status"] not in ("open", "claimed"):
            raise ValueError(f"intervention already {rec['status']}")
        return self.update(iid, status=decision, notes=notes, resolved_at=_now())


class HandoffController:
    def __init__(self, surface: Any, evidence: Evidence, store: InterventionStore | None = None,
                 timeout_s: float = 900, announce: Callable[[str], None] = print):
        self.surface = surface
        self.evidence = evidence
        self.store = store or InterventionStore()
        self.timeout_s = timeout_s
        self.announce = announce

    def request(self, kind: str, reason: str, context: dict[str, Any], obs: Observation,
                resume_mode: str = "agent", cleared: Callable[[], bool] | None = None,
                idle_s: float = 3.0) -> dict[str, Any]:
        """Pause automation, hand the live session to a human, block until they resolve."""
        shot = self.evidence.screenshot(self.surface, obs, f"handoff-{kind}")
        self.surface.set_control("paused", "awaiting-operator")
        _, cursor = self.surface.events(0)
        iid = self.store.create({
            "kind": kind, "reason": reason, "context": self.evidence.redactor.obj(context),
            "run_id": self.evidence.run_id, "run_dir": str(self.evidence.dir),
            "screenshot": shot, "session": {"surface": getattr(self.surface, "kind", "?"),
                                            "bridge": getattr(self.surface, "base", "")},
        })
        if shot:
            shutil.copy(self.evidence.dir / shot, self.store.base / f"{iid}.png")
        self.evidence.log("handoff_requested", intervention=iid, kind=kind, reason=reason, context=context)
        self.announce(f"\n>>> HUMAN NEEDED [{kind}] {reason}\n    intervention: {iid}\n"
                      f"    open the operator console (python -m cua console) or run:\n"
                      f"    python -m cua operator claim {iid} --as <name>  ...then work in the CoreLink window\n"
                      f"    python -m cua operator resolve {iid} resume|approve|deny|abort\n")
        deadline = time.monotonic() + self.timeout_s
        claimed = False
        rec = self.store.get(iid)
        while time.monotonic() < deadline:
            rec = self.store.get(iid)
            if rec["status"] == "claimed" and not claimed:
                claimed = True
                self.surface.set_control("human", rec.get("operator", "operator"))
                self.evidence.log("handoff_claimed", intervention=iid, operator=rec.get("operator"))
            if rec["status"] in RESOLVED:
                break
            # Operators often just fix the problem in the app without touching the console. If the condition
            # that caused the escalation is gone and the human has been idle for a moment, resume by itself.
            if cleared is not None and rec["status"] in ("open", "claimed"):
                evs, _ = self.surface.events(cursor)
                human_evs = [e for e in evs if e.get("actor") == "human" and e.get("kind") in ("click", "type", "key")]
                if human_evs and time.time() * 1000 - human_evs[-1].get("ts", 0) > idle_s * 1000 and cleared():
                    rec = self.store.update(iid, status="resume", resolved_at=_now(),
                                            operator=rec.get("operator") or "operator (in app)",
                                            notes="auto-resumed: the operator cleared the condition in the app")
                    self.evidence.log("handoff_auto_resumed", intervention=iid)
                    break
            time.sleep(0.4)
        else:
            rec = self.store.update(iid, status="expired", resolved_at=_now())
        events, _ = self.surface.events(cursor)
        human = [self.evidence.redactor.obj(e) for e in events
                 if e.get("actor") == "human" or e.get("kind") in ("control_change", "window_opened", "window_closed",
                                                                  "blocked_input")]
        self.store.update(iid, human_events=human)
        decision = rec["status"] if rec["status"] in RESOLVED else "abort"
        result = {"intervention": iid, "kind": kind, "reason": reason, "decision": decision,
                  "operator": rec.get("operator"), "notes": rec.get("notes", ""),
                  "human_actions": sum(1 for e in human if e.get("actor") == "human"),
                  "expired": rec["status"] == "expired"}
        self.evidence.write_json(f"handoff-{iid}.json", {**result, "events": human})
        self.evidence.log("handoff_resolved", **result)
        self.surface.set_control(resume_mode, "automation")
        return result
