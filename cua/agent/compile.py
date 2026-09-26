"""Compile a discovery trace into a Capability artifact, decoupled from the model transcript.

What survives from the run: which element each action targeted (as ranked locators), where each
value came from, what changed on screen (as checkpoints), and the policy risk class. What does not
survive: the model's messages, refs, screenshots, and any concrete sensitive values.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..artifact import (AppRef, Capability, Checkpoint, Extraction, LiteralValue, OutcomeRef, OutputSpec,
                        ParamSpec, ParamValue, Provenance, RowMatch, Step, Target)
from ..locate import build_target, rx_escape
from ..profile import AppProfile
from ..redact import Redactor
from ..replay import visible_lines
from ..surface.base import Node, Observation
from .discover import DiscoveryResult, Task, TraceStep

ROOT = Path(__file__).resolve().parents[2]
TS = re.compile(r"^\d{2}:\d{2}:\d{2}\s+")


def generalize(line: str) -> str:
    """'01:02:03  INQUIRY COMPLETE - 2 SHARE(S)' -> 'INQUIRY COMPLETE - \\d+ SHARE\\(S\\)'"""
    core = TS.sub("", line.strip())
    return re.sub(r"\d+", lambda _: r"\d+", rx_escape(core))


def success_checks(proof_text: str | None, lines: set[str], redactor: Redactor) -> list[Checkpoint]:
    """Turn the model's quoted proof into checkpoints. The model may join several on-screen labels into one
    string, so split it on runs of whitespace/newlines and match each fragment to a visible line. Numbers are
    generalized, and lines that contain PII are never baked in."""
    out: list[Checkpoint] = []
    seen: set[str] = set()
    for frag in re.split(r"\s{2,}|\n", TS.sub("", (proof_text or "").strip())):
        frag = frag.strip()
        if len(frag) < 4:
            continue
        for l in sorted(lines):
            if frag in l and redactor.text(l) == l:
                pat = generalize(l)
                if pat not in seen:
                    seen.add(pat)
                    out.append(Checkpoint(kind="text_present", pattern=pat,
                                          description=f"screen shows '{TS.sub('', l)}'"))
                break
    return out


def describe(node: Node) -> str:
    what = node.label or node.name or node.role
    where = f" in '{node.container}'" if node.container else (f" in window '{node.window}'" if node.window else "")
    return f"{node.role} '{what}'{where}"


def derive_checkpoints(before: Observation, after: Observation | None, step: TraceStep, target: Target,
                       red: Redactor) -> list[Checkpoint]:
    if after is None:
        return []
    cps: list[Checkpoint] = []
    before_titles = {w.title for w in before.windows}
    for w in after.windows:
        if w.title not in before_titles and w.modal:
            cps.append(Checkpoint(kind="window_present", pattern=f"^{rx_escape(w.title)}$",
                                  description=f"dialog '{w.title}' is shown"))
    for c in after.containers():
        if c not in before.containers():
            cps.append(Checkpoint(kind="container_present", pattern=f"^{rx_escape(c)}$",
                                  description=f"work window '{c}' opened"))
    new_lines = visible_lines(after) - visible_lines(before)
    status = [l for l in new_lines if TS.match(l)]
    # Prefer the app's own status line; never bake PII or amounts into a checkpoint.
    for l in status[:1]:
        if red.text(l) == l:
            cps.append(Checkpoint(kind="text_present", pattern=generalize(l),
                                  description=f"status line shows '{TS.sub('', l)}'"))
    if step.tool in ("type_text", "select_option"):
        vs = step.value_source or {}
        pattern = ".+" if vs.get("source") == "param" else f"^{rx_escape(vs.get('value', ''))}$"
        cps.append(Checkpoint(kind="element_value", pattern=pattern,
                              description="field holds the entered value"))
    return cps


def compile_capability(task: Task, result: DiscoveryResult, profile: AppProfile, redactor: Redactor,
                       tenant: str | None) -> Capability:
    steps: list[Step] = []
    notes: list[str] = []
    n = 0
    for t in result.trace:
        if not t.ok:
            continue
        if t.tool == "ask_human":
            notes.append(f"Human intervened during discovery at trace step {t.index}: {t.args.get('reason')}. "
                         "Reviewer must confirm the recorded flow covers what the human did.")
            continue
        if t.tool in ("wait", "finish"):
            continue
        n += 1
        sid = f"s{n}"
        if t.tool == "press_key":
            steps.append(Step(id=sid, intent=t.why or f"Press {t.args['key']}", action="key",
                              key=t.args["key"].upper(), risk=_risk(t),
                              expect=derive_checkpoints(t.obs_before, t.obs_after, t, None, redactor)))  # type: ignore[arg-type]
            continue
        assert t.node is not None
        target = build_target(t.obs_before, t.node, profile.main_window, describe(t.node))
        if t.tool == "extract":
            ex = t.extracted or {}
            steps.append(Step(id=sid, intent=t.why or f"Read {ex.get('output')}", action="extract", target=target,
                              extract=Extraction(output=ex["output"], read=ex.get("read", "value"),
                                                 row_match=RowMatch(**ex["row_match"]) if ex.get("row_match") else None,
                                                 column=ex.get("column"), parse=ex.get("parse", "text"),
                                                 pattern=ex.get("pattern"))))
            continue
        action = {"click": "click", "type_text": "set_text", "select_option": "select"}[t.tool]
        vs = t.value_source
        value = None
        if vs:
            value = ParamValue(param=vs["param"]) if vs["source"] == "param" else LiteralValue(value=vs["value"])
        steps.append(Step(id=sid, intent=t.why or f"{action} {describe(t.node)}", action=action, target=target,
                          value=value, risk=_risk(t),
                          expect=derive_checkpoints(t.obs_before, t.obs_after, t, target, redactor)))

    success: list[Checkpoint] = []
    success = success_checks(result.proof_text, visible_lines(result.final_obs), redactor)
    risks = {s.risk for s in steps}
    overall = "irreversible" if "irreversible" in risks else "state_changing" if "risky" in risks else "read_only"
    outcome_desc = {r.code: r.message for r in profile.outcome_rules}
    return Capability(
        id=task.capability_id, version=task.version, status="draft", title=task.title,
        description=task.description,
        app=AppRef(product=profile.product, versions=profile.tenant(tenant).product_version or "*",
                   surface=profile.surface, profile=profile.id),
        inputs=[ParamSpec(**i) for i in task.inputs], outputs=[OutputSpec(**o) for o in task.outputs],
        business_outcomes=[OutcomeRef(code=c, description=outcome_desc.get(c, "")) for c in task.business_outcomes],
        risk=overall, steps=steps, success=success,
        provenance=Provenance(discovered_by=result.planner, discovery_run=result.run_id, goal=task.goal,
                              tenant=tenant, notes=notes))


def _risk(t: TraceStep) -> str:
    r = (t.decision or {}).get("risk", "safe")
    return r if r in ("safe", "risky", "irreversible") else "safe"


def save_capability(cap: Capability, directory: Path | None = None) -> Path:
    d = directory or ROOT / "capabilities"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{cap.id}.v{cap.version}.json"
    p.write_text(cap.to_json(), encoding="utf-8")
    return p


def load_capability(path_or_id: str) -> Capability:
    p = Path(path_or_id)
    if not p.exists():
        matches = sorted((ROOT / "capabilities").glob(f"{path_or_id}.v*.json"))
        if not matches:
            raise FileNotFoundError(f"no capability '{path_or_id}'")
        p = matches[-1]
    return Capability.model_validate_json(p.read_text(encoding="utf-8"))
