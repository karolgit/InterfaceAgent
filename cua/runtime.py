"""Wiring: build the objects a run needs, and manage the target app process."""
from __future__ import annotations

import json
import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .agent.compile import compile_capability, load_capability, save_capability
from .agent.discover import ClaudePlanner, DiscoveryAgent, GeminiPlanner, ScriptedPlanner, Task
from .artifact import Capability, RunResult
from .evidence import Evidence, new_run_id
from .handoff import HandoffController
from .policy import Policy
from .profile import AppProfile
from .redact import Redactor
from .replay import ReplayEngine
from .surface.swing import SwingSurface, launch_corelink

ROOT = Path(__file__).resolve().parents[1]
FAULTS = ROOT / "runs" / "faults.properties"


def write_faults(faults: dict[str, str] | None) -> None:
    FAULTS.parent.mkdir(parents=True, exist_ok=True)
    FAULTS.write_text("".join(f"{k}={v}\n" for k, v in (faults or {}).items()), encoding="utf-8")


@contextmanager
def app_session(tenant: str = "heritage", attach: bool = False, port: int = 8740, keep_open: bool = False,
                idle_timeout_s: int = 900) -> Iterator[SwingSurface]:
    if attach:
        s = SwingSurface(port)
        if not s.healthy():
            raise RuntimeError(f"no bridge on port {port}; start the app with `python -m cua app` first")
        yield s
        return
    FAULTS.parent.mkdir(parents=True, exist_ok=True)
    proc, s = launch_corelink(tenant=tenant, port=port, faults_file=FAULTS, idle_timeout_s=idle_timeout_s,
                              log_file=ROOT / "runs" / "corelink.log")
    try:
        yield s
    finally:
        if not keep_open:
            proc.kill()


def make_redactor(profile: AppProfile) -> Redactor:
    return Redactor(pii_labels=profile.pii_labels)


def run_discovery(task_path: str, surface: SwingSurface, tenant: str, planner_kind: str = "claude",
                  script_path: str | None = None, model: str | None = None,
                  handoff_timeout_s: float = 900, out_dir: Path | None = None) -> tuple[Any, Capability | None, Path | None]:
    task = Task.load(task_path)
    profile = AppProfile.load(task.app_profile)
    policy = Policy.load(ROOT / "configs" / "policies" / f"{task.app_profile}.yaml")
    red = make_redactor(profile)
    ev = Evidence(new_run_id("discover"), red, base=out_dir)
    handoff = HandoffController(surface, ev, timeout_s=handoff_timeout_s)
    ReplayEngine(surface, profile, policy, ev, red, handoff, tenant=tenant).prepare_session()
    if planner_kind == "claude":
        planner: Any = ClaudePlanner(ev, model=model) if model else ClaudePlanner(ev)
    elif planner_kind == "gemini":
        planner = GeminiPlanner(ev, model=model) if model else GeminiPlanner(ev)
    else:
        planner = ScriptedPlanner(json.loads(Path(script_path).read_text()))
    agent = DiscoveryAgent(surface, profile, policy, task, ev, red, handoff)
    result = agent.run(planner)
    ev.write_json("discovery_summary.json", {
        "run_id": result.run_id, "planner": result.planner, "success": result.success, "summary": result.summary,
        "proof_text": result.proof_text, "steps": len(result.trace), "usage": result.usage,
        "trace": [{"i": t.index, "tool": t.tool, "args": {k: v for k, v in t.args.items() if k != "why"},
                   "why": t.why, "ok": t.ok, "result": t.result, "decision": t.decision,
                   "target": t.node and {"role": t.node.role, "name": t.node.name, "label": t.node.label,
                                         "container": t.node.container, "window": t.node.window}}
                  for t in result.trace]})
    if not result.success:
        return result, None, None
    cap = compile_capability(task, result, profile, red, tenant)
    path = save_capability(cap)
    shutil.copy(path, ev.dir / path.name)
    ev.log("capability_saved", path=str(path.relative_to(ROOT)), steps=len(cap.steps))
    return result, cap, path


def run_replay(cap: Capability, params: dict[str, Any], surface: SwingSurface, tenant: str,
               approve_irreversible: bool = False, allow_draft: bool = False, handoff_timeout_s: float = 900,
               out_dir: Path | None = None, run_prefix: str = "replay") -> RunResult:
    profile = AppProfile.load(cap.app.profile)
    policy = Policy.load(ROOT / "configs" / "policies" / f"{cap.app.profile}.yaml")
    red = make_redactor(profile)
    ev = Evidence(new_run_id(run_prefix), red, base=out_dir)
    handoff = HandoffController(surface, ev, timeout_s=handoff_timeout_s)
    engine = ReplayEngine(surface, profile, policy, ev, red, handoff, tenant=tenant,
                          approve_irreversible=approve_irreversible)
    return engine.run(cap, params, allow_draft=allow_draft)


__all__ = ["app_session", "run_discovery", "run_replay", "write_faults", "load_capability"]
