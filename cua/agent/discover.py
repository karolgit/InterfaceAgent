"""Goal-driven discovery loop: observe -> decide (LLM) -> policy -> act -> record.

The loop is planner-agnostic. ClaudePlanner is the real decision-maker. ScriptedPlanner replays a
fixed list of tool calls and exists ONLY for offline tests of the harness; runs made with it are
labeled `discovered_by: scripted-test` and are never presented as LLM discovery evidence.
"""
from __future__ import annotations

import base64
import io
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import yaml
from PIL import Image

from ..evidence import Evidence, new_run_id
from ..handoff import HandoffController
from ..policy import Policy, Verdict
from ..profile import AppProfile
from ..redact import Redactor
from ..surface.base import STRUCTURAL_ROLES, Node, Observation
from .tools import SYSTEM_PROMPT, TOOLS

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODEL = os.environ.get("CUA_MODEL", "claude-opus-5")


# ----------------------------------------------------------------------------- task spec

@dataclass
class Task:
    capability_id: str
    title: str
    description: str
    app_profile: str
    goal: str
    inputs: list[dict[str, Any]]
    outputs: list[dict[str, Any]]
    business_outcomes: list[str]
    example_inputs: dict[str, str]
    max_steps: int = 25
    version: str = "1.0.0"

    @staticmethod
    def load(path: str | Path) -> "Task":
        d = yaml.safe_load(Path(path).read_text())
        return Task(**d)

    def goal_text(self) -> str:
        names = ", ".join(f"`{i['name']}`" for i in self.inputs) or "none"
        outs = ", ".join(f"`{o['name']}` ({o.get('type', 'string')}): {o.get('description', '')}" for o in self.outputs)
        return (f"GOAL: {self.goal}\n\nDeclared inputs (use by name via `param`): {names}\n"
                f"Declared outputs to extract: {outs or 'none'}")


# ----------------------------------------------------------------------------- trace

@dataclass
class TraceStep:
    index: int
    tool: str
    args: dict[str, Any]
    why: str
    node: Node | None
    obs_before: Observation
    obs_after: Observation | None
    decision: dict[str, str] | None
    ok: bool
    result: str
    value_source: dict[str, str] | None = None
    extracted: dict[str, Any] | None = None
    handoff: dict[str, Any] | None = None


@dataclass
class DiscoveryResult:
    run_id: str
    success: bool
    summary: str
    proof_text: str | None
    trace: list[TraceStep]
    outputs: dict[str, Any]
    final_obs: Observation
    evidence: Evidence
    planner: str
    usage: dict[str, int] = field(default_factory=dict)


# ----------------------------------------------------------------------------- rendering for the model

def render_observation(obs: Observation, red: Redactor) -> str:
    sensitive = red.sensitive_nodes(obs)
    lines = []
    top = obs.top_window
    for w in obs.windows:
        flag = " [MODAL - on top, must be handled first]" if (w.modal and w is top) else ""
        lines.append(f"== window \"{w.title}\" ({w.kind}){flag}")
        for n in w.nodes:
            if n.role in STRUCTURAL_ROLES and not n.name:
                continue
            if n.role in {"frame", "dialog", "menu bar", "tool bar", "option pane", "separator",
                          "popup menu"} and not n.name:
                continue
            if n.role in {"frame", "dialog"}:
                continue
            name, value = red.node_text(n, sensitive)
            ind = "  " * min(max(n.depth - 3, 0), 6)
            desc = f"{ind}[{n.ref}] {n.role}"
            if name:
                desc += f" \"{name}\""
            if n.label and n.label != n.name:
                desc += f" label=\"{n.label}\""
            if value is not None and n.role not in {"label"}:
                desc += f" value=\"{value}\""
            if n.options:
                desc += f" options={n.options}"
            if n.columns:
                desc += f" columns={n.columns}"
            if n.rows is not None:
                rows = [[red.text(c) for c in r] for r in n.rows]
                desc += f" rows={rows}"
            if "enabled" not in n.states and n.role in {"push button", "text", "menu item", "combo box"}:
                desc += " (disabled)"
            if n.hidden:
                desc += " (in closed menu; clickable)"
            if n.container and n.role == "internal frame":
                desc += "  <- open work window"
            lines.append(desc)
    return "\n".join(lines)


def screenshot_for_model(surface: Any, obs: Observation, red: Redactor, max_w: int = 1280) -> bytes:
    png, origin = surface.screenshot()
    png = red.screenshot(png, origin, obs)
    img = Image.open(io.BytesIO(png))
    if img.width > max_w:
        img = img.resize((max_w, int(img.height * max_w / img.width)))
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


# ----------------------------------------------------------------------------- planners

@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


class Planner(Protocol):
    name: str

    def start(self, task: Task) -> None: ...

    def decide(self, obs_text: str, png: bytes, results: list[tuple[ToolCall, str, bool]]) -> list[ToolCall]: ...


class ClaudePlanner:
    """Claude via the official SDK, manual tool loop, append-only history."""

    def __init__(self, evidence: Evidence, model: str = DEFAULT_MODEL, effort: str = "high"):
        import anthropic

        self.client = anthropic.Anthropic()
        self.model = model
        self.effort = effort
        self.name = f"llm:{model}"
        self.ev = evidence
        self.messages: list[dict[str, Any]] = []
        self.usage = {"input_tokens": 0, "output_tokens": 0}
        self._pending_goal: str | None = None

    def start(self, task: Task) -> None:
        self._pending_goal = task.goal_text()

    def decide(self, obs_text: str, png: bytes, results: list[tuple[ToolCall, str, bool]]) -> list[ToolCall]:
        content: list[dict[str, Any]] = []
        for call, text, is_error in results:
            content.append({"type": "tool_result", "tool_use_id": call.id, "content": text, "is_error": is_error})
        if self._pending_goal:
            content.append({"type": "text", "text": self._pending_goal})
            self._pending_goal = None
        content.append({"type": "text", "text": "CURRENT SCREEN (accessibility outline):\n" + obs_text})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                     "data": base64.b64encode(png).decode()}})
        self.messages.append({"role": "user", "content": content})
        resp = self.client.beta.messages.create(
            model=self.model, max_tokens=16000, system=SYSTEM_PROMPT, tools=TOOLS, messages=self.messages,
            thinking={"type": "adaptive", "display": "summarized"},
            output_config={"effort": self.effort},
            betas=["server-side-fallback-2026-07-01"], extra_body={"fallbacks": "default"})
        self.usage["input_tokens"] += resp.usage.input_tokens
        self.usage["output_tokens"] += resp.usage.output_tokens
        if resp.stop_reason == "refusal":
            raise RuntimeError(f"model refused: {getattr(resp, 'stop_details', None)}")
        self.messages.append({"role": "assistant", "content": [b.model_dump(exclude_none=True) for b in resp.content]})
        for b in resp.content:
            if b.type == "thinking" and getattr(b, "thinking", ""):
                self.ev.log("model_thinking_summary", text=b.thinking[:2000])
            elif b.type == "text" and b.text.strip():
                self.ev.log("model_text", text=b.text[:2000])
        calls = [ToolCall(b.id, b.name, dict(b.input)) for b in resp.content if b.type == "tool_use"]
        if not calls and resp.stop_reason != "tool_use":
            # Model answered in prose; nudge it back to the tool protocol on the next turn.
            self.messages.append({"role": "user", "content": [{"type": "text", "text":
                                  "Please continue by calling exactly one tool."}]})
            self.messages.append({"role": "assistant", "content": [{"type": "text", "text": "Understood."}]})
        return calls


GEMINI_DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")


class GeminiPlanner:
    """Google Gemini via the google-genai SDK. Same tools, same redacted inputs, manual function-call loop."""

    def __init__(self, evidence: Evidence, model: str = GEMINI_DEFAULT_MODEL):
        from google import genai
        from google.genai import types

        self.types = types
        self.client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))
        self.model = model
        self.name = f"llm:{model}"
        self.ev = evidence
        self.history: list[Any] = []
        self.usage = {"input_tokens": 0, "output_tokens": 0}
        self._pending_goal: str | None = None
        self._nudge = False
        decls = []
        for t in TOOLS:
            schema = json.loads(json.dumps(t["input_schema"]))
            schema.pop("additionalProperties", None)
            decls.append(types.FunctionDeclaration(name=t["name"], description=t["description"],
                                                   parameters_json_schema=schema))
        self.config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            tools=[types.Tool(function_declarations=decls)],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            thinking_config=types.ThinkingConfig(include_thoughts=True))

    def start(self, task: Task) -> None:
        self._pending_goal = task.goal_text()

    def decide(self, obs_text: str, png: bytes, results: list[tuple[ToolCall, str, bool]]) -> list[ToolCall]:
        T = self.types
        parts = [T.Part.from_function_response(name=call.name,
                                               response={"error" if is_error else "result": text})
                 for call, text, is_error in results]
        if self._nudge:
            parts.append(T.Part.from_text(text="Please continue by calling exactly one tool."))
            self._nudge = False
        if self._pending_goal:
            parts.append(T.Part.from_text(text=self._pending_goal))
            self._pending_goal = None
        parts.append(T.Part.from_text(text="CURRENT SCREEN (accessibility outline):\n" + obs_text))
        parts.append(T.Part.from_bytes(data=png, mime_type="image/png"))
        self.history.append(T.Content(role="user", parts=parts))
        resp = self.client.models.generate_content(model=self.model, contents=self.history, config=self.config)
        um = resp.usage_metadata
        if um:
            self.usage["input_tokens"] += um.prompt_token_count or 0
            self.usage["output_tokens"] += (um.candidates_token_count or 0) + (um.thoughts_token_count or 0)
        cand = resp.candidates[0] if resp.candidates else None
        if cand is None or cand.content is None:
            raise RuntimeError(f"Gemini returned no content (finish_reason="
                               f"{getattr(cand, 'finish_reason', None)}, feedback={resp.prompt_feedback})")
        self.history.append(cand.content)
        for p in cand.content.parts or []:
            if p.text and p.thought:
                self.ev.log("model_thinking_summary", text=p.text[:2000])
            elif p.text:
                self.ev.log("model_text", text=p.text[:2000])
        calls = [ToolCall(fc.id or f"g-{len(self.history)}-{i}", fc.name, dict(fc.args or {}))
                 for i, fc in enumerate(resp.function_calls or [])]
        if not calls:
            self._nudge = True
        return calls


class ScriptedPlanner:
    """OFFLINE TEST DOUBLE. Resolves each scripted action's target by a predicate over the outline."""

    def __init__(self, script: list[dict[str, Any]]):
        self.script = list(script)
        self.name = "scripted-test"
        self.obs: Observation | None = None
        self.usage: dict[str, int] = {}
        self._n = 0

    def start(self, task: Task) -> None:
        pass

    def bind(self, obs: Observation) -> None:
        self.obs = obs

    def decide(self, obs_text: str, png: bytes, results: list[tuple[ToolCall, str, bool]]) -> list[ToolCall]:
        if not self.script:
            return [ToolCall("s-end", "finish", {"success": False, "summary": "script exhausted"})]
        spec = dict(self.script.pop(0))
        find = spec.pop("find", None)
        if find is not None and self.obs is not None:
            node = next((n for n in self.obs.nodes() if all(
                (getattr(n, k) or "") == v if not k.endswith("_contains") else v in (getattr(n, k[:-9]) or "")
                for k, v in find.items())), None)
            if node is None:
                raise RuntimeError(f"scripted target not found: {find}")
            spec["input"]["ref"] = node.ref
        self._n += 1
        return [ToolCall(f"s-{self._n}", spec["tool"], spec["input"])]


# ----------------------------------------------------------------------------- the loop

class DiscoveryAgent:
    def __init__(self, surface: Any, profile: AppProfile, policy: Policy, task: Task,
                 evidence: Evidence, redactor: Redactor, handoff: HandoffController):
        self.s = surface
        self.profile = profile
        self.policy = policy
        self.task = task
        self.ev = evidence
        self.red = redactor
        self.handoff = handoff
        self.outputs: dict[str, Any] = {}
        self.trace: list[TraceStep] = []
        self.approved = False  # one human approval covers the irreversible transaction in this run
        sensitive_inputs = {i["name"] for i in task.inputs if i.get("sensitive")}
        for k, v in task.example_inputs.items():
            if k in sensitive_inputs:
                self.red.register(v, k)

    def run(self, planner: Planner, timeout_s: float = 600) -> DiscoveryResult:
        self.ev.log("discovery_started", goal=self.task.goal, capability=self.task.capability_id,
                    planner=planner.name, max_steps=self.task.max_steps)
        self.s.set_control("agent", f"discovery:{self.ev.run_id}")
        planner.start(self.task)
        results: list[tuple[ToolCall, str, bool]] = []
        success, summary, proof = False, "stopped: max steps reached", None
        t_end = time.monotonic() + timeout_s
        try:
            for turn in range(1, self.task.max_steps + 1):
                if time.monotonic() > t_end:
                    summary = "stopped: timeout"
                    break
                obs = self.s.settle()
                self.red.learn(obs)
                if isinstance(planner, ScriptedPlanner):
                    planner.bind(obs)
                obs_text = render_observation(obs, self.red)
                png = screenshot_for_model(self.s, obs, self.red)
                (self.ev.dir / f"turn-{turn:02d}.png").write_bytes(png)
                self.ev.log("observation", turn=turn, windows=[w.title for w in obs.windows],
                            containers=obs.containers())
                calls = planner.decide(obs_text, png, results)
                results = []
                if not calls:
                    continue
                for i, call in enumerate(calls):
                    if i > 0:
                        results.append((call, "Ignored: take one action per turn, then observe.", True))
                        continue
                    if call.name == "finish":
                        success = bool(call.input.get("success"))
                        summary = call.input.get("summary", "")
                        proof = call.input.get("proof_text")
                        self.ev.log("finish", success=success, summary=summary, proof_text=proof)
                        results.append((call, "Run ended.", False))
                        return self._result(planner, success, summary, proof)
                    text, ok = self._execute(turn, call, obs)
                    results.append((call, text, not ok))
            return self._result(planner, success, summary, proof)
        finally:
            self.s.set_control("free", "")

    def _result(self, planner: Any, success: bool, summary: str, proof: str | None) -> DiscoveryResult:
        missing = [o["name"] for o in self.task.outputs if o["name"] not in self.outputs]
        if success and missing:
            success, summary = False, f"finish(success) rejected: outputs not extracted: {missing}"
            self.ev.log("finish_rejected", missing=missing)
        final = self.s.observe()
        self.ev.screenshot(self.s, final, "discovery-final")
        return DiscoveryResult(self.ev.run_id, success, summary, proof, self.trace, dict(self.outputs), final,
                               self.ev, planner.name, getattr(planner, "usage", {}))

    # -- tool execution
    def _execute(self, turn: int, call: ToolCall, obs: Observation) -> tuple[str, bool]:
        a = call.input
        why = a.get("why", "")
        self.ev.log("decision", turn=turn, tool=call.name, args={k: v for k, v in a.items() if k != "why"}, why=why)
        node = obs.node(a["ref"]) if "ref" in a else None
        if "ref" in a and node is None:
            return self._record(turn, call, None, obs, None, False, f"No element with ref {a['ref']} on screen.")
        if call.name == "ask_human":
            res = self.handoff.request("stuck", a.get("reason", ""), {"goal": self.task.goal, "turn": turn}, obs,
                                       resume_mode="agent")
            msg = (f"Human operator {res.get('operator')} finished with decision '{res['decision']}' after "
                   f"{res['human_actions']} recorded actions. Notes: {res.get('notes', '')}. Control is back with you.")
            return self._record(turn, call, None, obs, None, res["decision"] in ("resume", "approve"), msg,
                                handoff=res)
        if call.name == "wait":
            time.sleep(min(float(a.get("seconds", 1)), 10))
            return self._record(turn, call, None, obs, None, True, "Waited.")
        op = {"click": "click", "type_text": "set_text", "select_option": "select", "press_key": "key",
              "extract": "extract"}.get(call.name)
        if op is None:
            return self._record(turn, call, node, obs, None, False, f"Unknown tool {call.name}.")
        decision = self.policy.check(op, node, a.get("key"))
        self.ev.log("policy", turn=turn, **decision.to_dict())
        if decision.verdict == Verdict.DENY:
            return self._record(turn, call, node, obs, decision.to_dict(), False, f"BLOCKED by policy: {decision.reason}")
        if decision.verdict == Verdict.REQUIRE_APPROVAL and not self.approved:
            res = self.handoff.request("approval", f"Discovery wants to perform an irreversible action: "
                                                   f"{call.name} '{node.name if node else ''}'",
                                       {"goal": self.task.goal, "turn": turn, "why": why}, obs)
            if res["decision"] != "approve":
                return self._record(turn, call, node, obs, decision.to_dict(), False,
                                    f"Operator did not approve (decision: {res['decision']}).", handoff=res)
            self.approved = True
        try:
            vs, extracted = None, None
            if op == "click":
                self.s.act("click", ref=node.ref)
            elif op in ("set_text", "select"):
                value, vs = self._value(a)
                if op == "set_text":
                    self.s.act("set_text", ref=node.ref, text=value)
                else:
                    self.s.act("select", ref=node.ref, option=value)
            elif op == "key":
                self.s.act("key", key=a["key"])
            elif op == "extract":
                extracted = self._extract(a, node)
                return self._record(turn, call, node, obs, decision.to_dict(), True,
                                    f"Captured output '{a['output']}' (value hidden by design).", extracted=extracted)
            after = self.s.settle()
            self.red.learn(after)
            changed = self._diff(obs, after)
            return self._record(turn, call, node, obs, decision.to_dict(), True, f"Done. {changed}", after=after,
                                value_source=vs)
        except Exception as e:  # surface errors go back to the model as tool errors
            return self._record(turn, call, node, obs, decision.to_dict(), False, f"Action failed: {e}")

    def _value(self, a: dict[str, Any]) -> tuple[str, dict[str, str]]:
        if a.get("param"):
            p = a["param"]
            if p not in self.task.example_inputs:
                raise ValueError(f"unknown param '{p}'")
            return self.task.example_inputs[p], {"source": "param", "param": p}
        text = a.get("text", a.get("option", ""))
        for k, v in self.task.example_inputs.items():  # model typed an input literally: bind it anyway
            if text == v:
                return v, {"source": "param", "param": k}
        return text, {"source": "literal", "value": text}

    def _extract(self, a: dict[str, Any], node: Node) -> dict[str, Any]:
        from ..replay import parse_value

        out = next((o for o in self.task.outputs if o["name"] == a["output"]), None)
        if out is None:
            raise ValueError(f"'{a['output']}' is not a declared output")
        spec: dict[str, Any] = {"output": a["output"], "parse": "currency" if out.get("type") == "currency" else "text"}
        if node.rows is not None:
            cols = node.columns or []
            rc, rv, col = a.get("row_column"), a.get("row_contains"), a.get("column")
            if not (rc and rv and col) or rc not in cols or col not in cols:
                raise ValueError(f"table extract needs row_column/row_contains/column from {cols}")
            rows = [r for r in node.rows if rv.lower() in r[cols.index(rc)].lower()]
            if len(rows) != 1:
                raise ValueError(f"{len(rows)} rows match {rc} contains '{rv}'")
            raw = rows[0][cols.index(col)]
            bound = next((k for k, v in self.task.example_inputs.items() if str(v) == rv), None)
            row = {"column": rc, "contains_param": bound} if bound else {"column": rc, "contains": rv}
            spec.update(read="table_cell", row_match=row, column=col)
        else:
            raw = node.value if node.value else node.name
            spec["read"] = "value" if node.value else "name"
        if a.get("pattern"):
            import re as _re
            m = _re.search(a["pattern"], raw or "")
            if not m:
                raise ValueError(f"pattern {a['pattern']!r} did not match the element text")
            raw = m.group(1) if m.groups() else m.group(0)
            spec["pattern"] = a["pattern"]
        val = parse_value(raw, spec["parse"])
        if out.get("sensitive"):
            self.red.register(val, a["output"])
            self.red.register(raw, a["output"])
        self.outputs[a["output"]] = val
        return spec

    def _diff(self, before: Observation, after: Observation) -> str:
        from ..replay import visible_lines

        new_win = [w.title for w in after.windows if w.title not in {x.title for x in before.windows}]
        gone = [w.title for w in before.windows if w.title not in {x.title for x in after.windows}]
        new_c = [c for c in after.containers() if c not in before.containers()]
        new_text = sorted(visible_lines(after) - visible_lines(before))[:6]
        parts = []
        if new_win:
            parts.append(f"new windows: {new_win}")
        if gone:
            parts.append(f"closed windows: {gone}")
        if new_c:
            parts.append(f"opened: {new_c}")
        if new_text:
            parts.append("new text: " + "; ".join(self.red.text(t) or "" for t in new_text))
        return " | ".join(parts) if parts else "No visible change."

    def _record(self, turn: int, call: ToolCall, node: Node | None, obs: Observation, decision: dict | None,
                ok: bool, result: str, after: Observation | None = None, value_source: dict | None = None,
                extracted: dict | None = None, handoff: dict | None = None) -> tuple[str, bool]:
        self.trace.append(TraceStep(len(self.trace), call.name, call.input, call.input.get("why", ""), node, obs,
                                    after, decision, ok, result, value_source, extracted, handoff))
        self.ev.log("action_result", turn=turn, tool=call.name, ok=ok, result=result,
                    target=node and {"role": node.role, "name": node.name, "label": node.label,
                                     "container": node.container, "window": node.window})
        return result, ok
