"""SIMULATED human operator, for unattended demos and tests ONLY.

A real operator uses the web console (python -m cua console) or the CLI and works directly in the
CoreLink window. This script stands in for them when nobody is at the keyboard: it claims open
interventions and performs "human" actions by injecting real input events into the app through the
bridge's human_* verbs, so the bridge records them exactly as it records a person's clicks and typing.
Every intervention it touches is annotated with `operator: sim-*` so evidence is never mistaken for
a real human.

    python scripts/sim_operator.py [--approve] [--supervisor] [--deny] [--once] [--timeout 120]
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cua.handoff import InterventionStore  # noqa: E402
from cua.surface.swing import SwingSurface  # noqa: E402


def key_supervisor_override(s: SwingSurface) -> str:
    """Type the FAKE supervisor credentials into the override dialog like a person would."""
    obs = s.observe()
    dlg = next((w for w in obs.windows if w.title == "Supervisor Override Required"), None)
    if dlg is None:
        return "override dialog not found; nothing to do"
    fields = [n for n in dlg.nodes if n.role in ("text", "password text")]
    ok = next(n for n in dlg.nodes if n.role == "push button" and n.name == "OK")
    s.act("human_click", ref=fields[0].ref)
    time.sleep(0.3)
    s.act("human_type", ref=fields[0].ref, text="sup01")
    time.sleep(0.3)
    s.act("human_click", ref=fields[1].ref)
    s.act("human_type", ref=fields[1].ref, text="demo123")
    time.sleep(0.3)
    s.observe()
    s.act("human_click", ref=ok.ref)
    time.sleep(0.8)
    return "keyed supervisor override (sup01) and pressed OK"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--approve", action="store_true", help="approve irreversible-step requests")
    ap.add_argument("--deny", action="store_true", help="deny irreversible-step requests")
    ap.add_argument("--supervisor", action="store_true", help="handle supervisor-override escalations")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--timeout", type=float, default=120)
    ap.add_argument("--port", type=int, default=int(__import__("os").environ.get("CUA_PORT", "8740")))
    args = ap.parse_args()
    store = InterventionStore()
    seen: set[str] = {r["id"] for r in store.list()}
    s = SwingSurface(args.port)
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        for rec in store.list("open"):
            if rec["id"] in seen:
                continue
            seen.add(rec["id"])
            kind, reason = rec["kind"], rec["reason"]
            print(f"[sim-operator] picked up {rec['id']} ({kind}): {reason}")
            if kind == "approval" and (args.approve or args.deny):
                store.claim(rec["id"], "sim-supervisor")
                time.sleep(1.0)
                decision = "approve" if args.approve else "deny"
                store.resolve(rec["id"], decision, "SIMULATED operator reviewed the Review screen and "
                                                   f"chose to {decision}.")
                print(f"[sim-operator] {decision}d")
            elif kind == "escalation" and args.supervisor and "SUPERVISOR_OVERRIDE" in reason:
                store.claim(rec["id"], "sim-supervisor")
                time.sleep(1.0)  # give the engine time to hand over control
                note = key_supervisor_override(s)
                store.resolve(rec["id"], "resume", f"SIMULATED operator: {note}")
                print(f"[sim-operator] {note}; resumed")
            else:
                print("[sim-operator] not configured for this request; leaving it for a human")
                continue
            if args.once:
                return 0
        time.sleep(0.4)
    return 0


if __name__ == "__main__":
    sys.exit(main())
