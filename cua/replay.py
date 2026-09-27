"""Deterministic replay: run a capability artifact with no LLM in the loop.

Per step:
  1. Resolve the target by trying its ranked locator strategies (waiting up to the step timeout,
     while watching outcome rules so a dialog or error is never mistaken for "element missing").
  2. Policy check (allowlist + risk). Irreversible steps need an approval grant or a human.
  3. Act through the surface.
  4. Wait for the step's checkpoints, again watching outcome rules. Only text that NEWLY appeared
     since the action counts for text rules, so stale status lines never trigger false outcomes.

Rule kinds:
  business_outcome -> stop, return status=business_outcome with the code (e.g. MEMBER_NOT_FOUND)
  recoverable      -> dismiss / wait / re-sign-on-and-restart, bounded, recorded in `recoveries`
  hard_failure     -> stop, return status=failed with step, expected, observed, and evidence
  escalate         -> hand the live session to a human; re-verify the checkpoint after
"""
from __future__ import annotations

import os
import re
import time
from decimal import Decimal, InvalidOperation
from typing import Any

from .artifact import Capability, Checkpoint, Failure, OutcomeRule, RunResult, Step
from .evidence import Evidence
from .handoff import HandoffController
from .locate import resolve, scope_window
from .policy import Policy, Verdict
from .profile import AppProfile
from .redact import Redactor, fingerprint
from .secrets import SecretStore
from .surface.base import Observation, SurfaceError

TEXT_ROLES = {"label", "table", "text", "option pane", "internal frame", "dialog"}


class _Restart(Exception):
    pass


class _Stop(Exception):
    def __init__(self, result: RunResult):
        self.result = result


def visible_lines(obs: Observation) -> set[str]:
    """Text a human would read: labels, table rows, dialog titles. Excludes our own typed input."""
    lines = {w.title for w in obs.windows}
    for n in obs.nodes():
        if n.hidden or n.role in {"text", "password text", "combo box"}:
            continue
        if n.rows:
            lines.update(" | ".join(r) for r in n.rows)
        if n.name and n.role in TEXT_ROLES:
            lines.update(l.strip() for l in n.name.splitlines() if l.strip())
    return lines


def parse_value(raw: str, how: str) -> Any:
    if how == "currency":
        s = raw.replace("$", "").replace(",", "").strip()
        neg = s.startswith("(") and s.endswith(")")
        try:
            d = Decimal(s.strip("()"))
        except InvalidOperation:
            raise ValueError(f"not a currency value: {raw!r}")
        return str(-d if neg else d)
    if how == "integer":
        return int(re.sub(r"[^\d-]", "", raw))
    return raw.strip()


class ReplayEngine:
    def __init__(self, surface: Any, profile: AppProfile, policy: Policy, evidence: Evidence,
                 redactor: Redactor, handoff: HandoffController, tenant: str | None = None,
                 approve_irreversible: bool = False, secrets: SecretStore | None = None,
                 max_restarts: int = 1):
        self.s = surface
        self.profile = profile
        self.policy = policy
        self.ev = evidence
        self.red = redactor
        self.handoff = handoff
        self.tenant = tenant
        self.tov = profile.tenant(tenant)
        self.approved = approve_irreversible
        self.approval_source = "caller_grant" if approve_irreversible else None
        self.secrets = secrets or SecretStore()
        self.max_restarts = max_restarts
        # Demo/recording aid only: pause after each action so viewers can follow. Never set in production.
        self.demo_delay_s = int(os.environ.get("CUA_DEMO_DELAY_MS", "0")) / 1000
        self.rules: list[OutcomeRule] = []
        self.rule_hits: dict[str, int] = {}
        self.baseline: Observation | None = None
        self.result = RunResult(run_id=evidence.run_id, capability="", status="failed",
                                evidence_dir=str(evidence.dir))

    # ------------------------------------------------------------------ entry point
    def run(self, cap: Capability, params: dict[str, Any], allow_draft: bool = False) -> RunResult:
        t0 = time.monotonic()
        self.result.capability = f"{cap.id}@{cap.version}"
        self.rules = self.profile.rules_for(self.tenant, cap.outcome_rules, cap.inherit_outcome_rules)
        for spec in cap.inputs:  # register sensitive inputs BEFORE anything is logged
            if spec.sensitive and params.get(spec.name) not in (None, ""):
                self.red.register(str(params[spec.name]), spec.name)
        self.ev.log("replay_started", capability=self.result.capability, tenant=self.tenant,
                    params=params, status=cap.status)
        try:
            self._validate(cap, params, allow_draft)
            self.s.set_control("agent", f"replay:{self.ev.run_id}")
            restarts = 0
            while True:
                try:
                    self._ensure_session()
                    self._reset_workspace()
                    outputs: dict[str, Any] = {}
                    for step in cap.steps:
                        self._run_step(cap, step, params, outputs)
                    self._verify_success(cap, outputs)
                    self._finish_success(cap, outputs)
                    break
                except _Restart:
                    restarts += 1
                    if restarts > self.max_restarts:
                        self._fail("RESTART_LIMIT", None, "capability to complete after re-sign-on",
                                   "session kept expiring")
                    self.ev.log("capability_restart", attempt=restarts)
        except _Stop as stop:
            self.result = stop.result
        finally:
            try:
                self.s.set_control("free", "")
            except Exception:
                pass
        self.result.duration_ms = int((time.monotonic() - t0) * 1000)
        persisted = self.result.model_dump()
        for o in cap.outputs:  # never persist sensitive outputs; fingerprint them for verification
            if o.sensitive and o.name in persisted["outputs"]:
                persisted["outputs"][o.name] = fingerprint(str(persisted["outputs"][o.name]))
        self.ev.write_json("result.json", persisted)
        self.ev.log("replay_finished", status=self.result.status,
                    outcome=self.result.outcome, failure=self.result.failure and self.result.failure.model_dump(),
                    duration_ms=self.result.duration_ms)
        return self.result

    def prepare_session(self) -> None:
        """Sign on if needed and clear the workspace (used before discovery too)."""
        self.rules = self.profile.rules_for(self.tenant, [], True)
        try:
            self.s.set_control("agent", "session-prep")
            self._ensure_session()
            self._reset_workspace()
        except _Stop as stop:
            raise RuntimeError(f"session preparation failed: {stop.result.failure or stop.result.outcome}")
        finally:
            self.s.set_control("free", "")

    # ------------------------------------------------------------------ input contract
    def _validate(self, cap: Capability, params: dict[str, Any], allow_draft: bool) -> None:
        if cap.status != "approved" and not allow_draft:
            self._reject("CAPABILITY_NOT_APPROVED", f"capability status is '{cap.status}'; unattended replay "
                                                    "requires 'approved' (use --allow-draft for supervised testing)")
        errors = []
        for spec in cap.inputs:
            if params.get(spec.name) in (None, "") and spec.default is not None:
                params[spec.name] = spec.default
            v = params.get(spec.name)
            if v is None or v == "":
                if spec.required:
                    errors.append(f"{spec.name}: required")
                continue
            sv = str(v)
            if spec.sensitive:
                self.red.register(sv, spec.name)
            if spec.type == "integer" and not re.fullmatch(r"-?\d+", sv):
                errors.append(f"{spec.name}: must be an integer")
            if spec.type == "decimal":
                try:
                    Decimal(sv)
                except InvalidOperation:
                    errors.append(f"{spec.name}: must be a decimal")
            if spec.enum and sv not in spec.enum:
                errors.append(f"{spec.name}: must be one of {spec.enum}")
            if spec.pattern and not re.fullmatch(spec.pattern, sv):
                errors.append(f"{spec.name}: does not match {spec.pattern}")
        unknown = set(params) - {p.name for p in cap.inputs}
        if unknown:
            errors.append(f"unknown inputs: {sorted(unknown)}")
        if errors:
            self._reject("INVALID_INPUT", "; ".join(errors))

    def _reject(self, code: str, msg: str) -> None:
        self.ev.log("rejected", code=code, message=msg)
        r = self.result.model_copy(update={"status": "rejected", "failure": Failure(code=code, observed=msg)})
        raise _Stop(r)

    # ------------------------------------------------------------------ session + workspace
    def _ensure_session(self) -> None:
        obs = self.s.observe()
        for w in obs.windows:
            if w.modal and not re.search(self.profile.sign_on_window, w.title):
                rule = self._match_dialog_rule(obs)
                if rule and rule[0].kind == "recoverable":
                    self._recover(rule[0], rule[1], obs, None)
                    obs = self.s.settle()
                elif rule and rule[0].recovery.kind == "resign_on_and_restart":
                    pass
                else:
                    self._fail("PRECONDITION_FAILED", None, "no unexpected modal dialog before start",
                               f"dialog '{w.title}' is open", obs)
        if any(re.search(self.profile.sign_on_window, w.title) for w in obs.windows):
            self.ev.log("sign_on_started")
            outputs: dict[str, Any] = {}
            for step in self.profile.sign_on:
                self._run_step(None, step, {}, outputs)
            self.ev.log("sign_on_completed")

    def _reset_workspace(self) -> None:
        """Start every run from the same state: main window, no open work frames."""
        obs = self.s.observe()
        frames = [n for n in obs.nodes() if n.role == "internal frame"]
        for f in frames:
            self.s.act("close", ref=f.ref)
        if frames:
            self.s.settle(timeout_s=2)
            self.ev.log("workspace_reset", closed=[f.name for f in frames])
        self.baseline = self.s.observe()

    # ------------------------------------------------------------------ steps
    def _run_step(self, cap: Capability | None, step: Step, params: dict[str, Any], outputs: dict[str, Any]) -> None:
        self.ev.log("step_started", step=step.id, intent=step.intent, action=step.action)
        node = None
        obs = self.s.observe()
        if step.target is not None:
            node, obs = self._resolve_with_wait(step)
        # Policy gate
        op = step.action
        if op not in ("wait_for",):
            decision = self.policy.check(op, node, step.key)
            self.ev.log("policy", step=step.id, **decision.to_dict())
            if decision.verdict == Verdict.DENY:
                self._fail("POLICY_DENIED", step, "action permitted by policy", decision.reason, obs)
            if decision.verdict == Verdict.REQUIRE_APPROVAL and not self.approved:
                self._get_approval(cap, step, obs)
            if decision.verdict == Verdict.REQUIRE_APPROVAL:
                self.ev.log("irreversible_action", step=step.id, approval=self.approval_source)
        # Act
        self.baseline = obs
        if op == "extract":
            assert step.extract and node
            val = self._extract(step, node, params)
            outputs[step.extract.output] = val
            if cap:
                spec = next((o for o in cap.outputs if o.name == step.extract.output), None)
                if spec and spec.sensitive:
                    self.red.register(val, step.extract.output)
            self.ev.log("extracted", step=step.id, output=step.extract.output, value=str(val))
            return
        try:
            if op == "click":
                self.s.act("click", ref=node.ref)
            elif op == "set_text":
                self.s.act("set_text", ref=node.ref, text=self._value(step, params))
            elif op == "select":
                self.s.act("select", ref=node.ref, option=self._value(step, params))
            elif op == "key":
                self.s.act("key", key=step.key)
        except SurfaceError as e:
            if op == "select" and "option not found" in str(e):
                # The app does not offer this value (e.g. a product another tenant has, or a share the member
                # lacks). That is a business outcome the caller can fix, so return the valid choices.
                shot = self.ev.screenshot(self.s, obs, "outcome-INVALID_OPTION")
                raise _Stop(self.result.model_copy(update={"status": "business_outcome", "outcome": {
                    "code": "INVALID_OPTION",
                    "message": f"'{self._value(step, params)}' is not an available choice for "
                               f"{step.target.description if step.target else 'this field'}.",
                    "available": list(node.options or []), "step_id": step.id, "screenshot": shot}}))
            self._fail("ACTION_FAILED", step, f"{op} to succeed", str(e), obs)
        if self.demo_delay_s:
            time.sleep(self.demo_delay_s)
        self.ev.log("acted", step=step.id, action=op, target=step.target and step.target.description,
                    value_source=step.value.source if step.value else None)
        self._await_checkpoints(step)

    def _value(self, step: Step, params: dict[str, Any]) -> str:
        v = step.value
        if v is None:
            return ""
        if v.source == "literal":
            return v.value
        if v.source == "param":
            return str(params[v.param])
        secret = self.secrets.get(v.secret)
        self.red.register(secret, "secret")
        return secret

    def _resolve_with_wait(self, step: Step):
        deadline = time.monotonic() + step.timeout_ms / 1000
        last_notes: list[str] = []
        obs = self.s.observe()
        while True:
            res = resolve(obs, step.target, self.profile.main_window, self.tov.label_aliases, self.tov.name_aliases)
            if res.node is not None:
                strat = step.target.strategies[res.strategy_index]
                self.ev.log("target_resolved", step=step.id, strategy=res.strategy_index, kind=strat.kind,
                            ref=res.node.ref)
                if res.strategy_index > 0:
                    fb = {"step": step.id, "strategy": res.strategy_index, "kind": strat.kind, "notes": res.notes}
                    self.result.locator_fallbacks.append(fb)
                    self.ev.log("locator_fallback", **fb)
                return res.node, obs
            last_notes = res.notes
            self._check_rules(obs, step)  # a dialog/error may explain why the target is missing
            if time.monotonic() > deadline:
                expected = f"{step.target.description} via " + ", ".join(
                    f"{s.kind}" for s in step.target.strategies)
                observed = self._describe(obs) + " | " + "; ".join(last_notes)
                self._fail("TARGET_NOT_FOUND", step, expected, observed, obs)
            time.sleep(0.2)
            obs = self.s.observe()

    def _await_checkpoints(self, step: Step) -> None:
        deadline = time.monotonic() + step.timeout_ms / 1000
        extended = 0
        if not step.expect:
            obs = self.s.settle(timeout_s=min(3.0, step.timeout_ms / 1000))
            self._check_rules(obs, step)
            return
        while True:
            obs = self.s.observe()
            handled = self._check_rules(obs, step)
            if handled == "wait" and extended < 5:
                deadline = max(deadline, time.monotonic() + 2.0)
                extended += 1
            unmet = [c for c in step.expect if not self._checkpoint(obs, c, step)]
            if not unmet:
                self.ev.log("checkpoint_met", step=step.id, checks=[c.description or c.kind for c in step.expect])
                return
            if time.monotonic() > deadline:
                self._fail("CHECKPOINT_NOT_MET", step,
                           "; ".join(f"{c.kind} {c.pattern or ''} {c.description}".strip() for c in unmet),
                           self._describe(obs), obs)
            time.sleep(0.15)

    def _checkpoint(self, obs: Observation, c: Checkpoint, step: Step | None = None) -> bool:
        if c.kind == "container_present":
            return any(re.search(c.pattern or "", n) for n in obs.containers())
        if c.kind == "window_present":
            return any(re.search(c.pattern or "", w.title) for w in obs.windows)
        if c.kind == "text_present":
            now = visible_lines(obs)
            new = now - (visible_lines(self.baseline) if self.baseline else set())
            if any(re.search(c.pattern or "", l) for l in new):
                return True
            # Same text can legitimately reappear (e.g. two runs in the same second produce an identical
            # timestamped status line). Accept it only if the screen actually changed since the action,
            # so a stale message from before the click can never satisfy the checkpoint on its own.
            return (self.baseline is not None and obs.fingerprint() != self.baseline.fingerprint()
                    and any(re.search(c.pattern or "", l) for l in now))
        if c.kind in ("element_value", "element_present"):
            target = c.target or (step.target if step else None)
            if target is None:
                return False
            res = resolve(obs, target, self.profile.main_window, self.tov.label_aliases, self.tov.name_aliases)
            if res.node is None:
                return False
            if c.pattern is None:
                return True
            return re.search(c.pattern, res.node.text() or "") is not None
        return False

    def _extract(self, step: Step, node, params: dict[str, Any] | None = None) -> Any:
        ex = step.extract
        if ex.read == "options":
            return [o for o in (node.options or []) if o]
        if ex.read == "table_cell":
            if not node.rows or not node.columns:
                self._fail("EXTRACTION_FAILED", step, "a table with rows", "table is empty")
            try:
                ci = node.columns.index(ex.column)
                mi = node.columns.index(ex.row_match.column)
            except ValueError:
                self._fail("EXTRACTION_FAILED", step, f"columns {ex.column}/{ex.row_match.column}",
                           f"columns {node.columns}")
            rm = ex.row_match
            want = str((params or {}).get(rm.contains_param, "")) if rm.contains_param else (rm.contains or "")
            rows = [r for r in node.rows if want and want.lower() in r[mi].lower()]
            if not rows and ex.no_match_outcome:
                r = self.result.model_copy(update={"status": "business_outcome", "outcome": {
                    "code": ex.no_match_outcome, "message": f"No row where {rm.column} contains the requested value.",
                    "step_id": step.id}})
                raise _Stop(r)
            if len(rows) != 1:
                self._fail("EXTRACTION_FAILED", step, f"exactly one row where {rm.column} contains "
                                                      f"'{want}'", f"{len(rows)} rows matched")
            raw = rows[0][ci]
        elif ex.read == "name":
            raw = node.name
        else:
            raw = node.value or ""
        if ex.pattern:
            m = re.search(ex.pattern, raw)
            if not m:
                self._fail("EXTRACTION_FAILED", step, f"text matching {ex.pattern}", "no match")
            raw = m.group(1) if m.groups() else m.group(0)
        try:
            return parse_value(raw, ex.parse)
        except ValueError as e:
            self._fail("EXTRACTION_FAILED", step, f"a {ex.parse} value", str(e))

    # ------------------------------------------------------------------ outcome rules
    def _match_dialog_rule(self, obs: Observation):
        for rule in self.rules:
            if rule.when_dialog:
                for w in obs.windows:
                    if w.modal and re.search(rule.when_dialog, w.title):
                        text = " ".join(n.name for n in w.nodes if n.role == "label" and n.name)
                        if rule.when_text and not re.search(rule.when_text, text):
                            continue
                        return rule, text
        return None

    def _match_rule(self, obs: Observation):
        m = self._match_dialog_rule(obs)
        if m:
            return m
        new = visible_lines(obs) - (visible_lines(self.baseline) if self.baseline else set())
        for rule in self.rules:
            if rule.when_text and not rule.when_dialog:
                for line in new:
                    if re.search(rule.when_text, line):
                        return rule, line
        return None

    def _check_rules(self, obs: Observation, step: Step | None) -> str | None:
        self.red.learn(obs)
        m = self._match_rule(obs)
        if not m:
            return None
        rule, text = m
        ui_text = re.sub(r"^\d{2}:\d{2}:\d{2}\s+", "", text.strip())
        self.ev.log("rule_matched", rule=rule.id, kind=rule.kind, code=rule.code, ui_text=ui_text,
                    step=step and step.id)
        if rule.kind == "recoverable":
            return self._recover(rule, ui_text, obs, step)
        if rule.kind == "business_outcome":
            shot = self.ev.screenshot(self.s, obs, f"outcome-{rule.code}")
            if rule.recovery.kind == "dismiss":
                self._dismiss(obs, rule.recovery.button)
            r = self.result.model_copy(update={
                "status": "business_outcome",
                "outcome": {"code": rule.code, "message": rule.message,
                            "ui_text": ui_text if rule.capture_text else None,
                            "step_id": step and step.id, "screenshot": shot}})
            raise _Stop(r)
        if rule.kind == "hard_failure":
            self._fail(rule.code, step, "step to complete", f"{rule.message} UI said: {ui_text}", obs,
                       dismiss=rule.recovery.button)
        if rule.kind == "escalate":
            ctx = {"capability": self.result.capability, "step": step and step.id,
                   "step_intent": step and step.intent, "rule": rule.id, "ui_text": ui_text}
            res = self.handoff.request("escalation", f"{rule.code}: {rule.message}", ctx, obs)
            self.result.handoffs.append(res)
            if res["decision"] in ("abort", "deny"):
                self._fail("HUMAN_ABORTED" if not res["expired"] else "HUMAN_TIMEOUT", step,
                           "operator to resolve the escalation", f"operator decision: {res['decision']}",
                           self.s.observe())
            # Human resolved it on the live session: refresh baseline so their state change counts.
            return "human"
        return None

    def _recover(self, rule: OutcomeRule, ui_text: str, obs: Observation, step: Step | None) -> str:
        if rule.recovery.kind == "wait":
            # A persisting "busy" indicator is one condition, not one per poll. Record it once per step;
            # the checkpoint loop bounds the total extra wait.
            key = f"wait:{rule.id}:{step and step.id}"
            if key not in self.rule_hits:
                self.rule_hits[key] = 1
                rec = {"rule": rule.id, "code": rule.code, "action": "wait", "attempt": 1,
                       "step": step and step.id, "ui_text": ui_text}
                self.result.recoveries.append(rec)
                self.ev.log("recovery", **rec)
            return "wait"
        n = self.rule_hits.get(rule.id, 0) + 1
        self.rule_hits[rule.id] = n
        if n > max(1, rule.recovery.max_attempts):
            self._fail("RECOVERY_EXHAUSTED", step, f"{rule.code} to clear after {rule.recovery.max_attempts} tries",
                       ui_text, obs)
        rec = {"rule": rule.id, "code": rule.code, "action": rule.recovery.kind, "attempt": n,
               "step": step and step.id, "ui_text": ui_text}
        self.result.recoveries.append(rec)
        self.ev.log("recovery", **rec)
        if rule.recovery.kind == "dismiss":
            self._dismiss(obs, rule.recovery.button)
            self.s.settle(timeout_s=2)
            return "dismissed"
        if rule.recovery.kind == "wait":
            return "wait"
        if rule.recovery.kind == "resign_on_and_restart":
            self._dismiss(obs, rule.recovery.button)
            self.s.settle(timeout_s=3)
            raise _Restart()
        return "none"

    def _dismiss(self, obs: Observation, button: str | None) -> None:
        for w in reversed(obs.windows):
            if not w.modal:
                continue
            for n in w.nodes:
                if n.role == "push button" and (button is None or n.name == button):
                    self.s.act("click", ref=n.ref)
                    self.ev.log("dismissed_dialog", window=w.title, button=n.name)
                    return

    # ------------------------------------------------------------------ approvals
    def _get_approval(self, cap: Capability | None, step: Step, obs: Observation) -> None:
        ctx = {"capability": self.result.capability, "step": step.id, "step_intent": step.intent,
               "screen": self._describe(obs)}
        res = self.handoff.request("approval", f"Irreversible step needs approval: {step.intent}", ctx, obs)
        self.result.handoffs.append(res)
        if res["decision"] == "approve":
            self.approved = True
            self.approval_source = f"operator:{res.get('operator')}"
            return
        if res["decision"] == "deny":
            r = self.result.model_copy(update={"status": "business_outcome", "outcome": {
                "code": "OPERATOR_DECLINED", "message": "A human operator declined the irreversible step.",
                "step_id": step.id}})
            raise _Stop(r)
        self._fail("HUMAN_TIMEOUT" if res["expired"] else "HUMAN_ABORTED", step, "approval decision",
                   "no operator responded in time" if res["expired"] else f"operator decision: {res['decision']}", obs)

    # ------------------------------------------------------------------ results
    def _verify_success(self, cap: Capability, outputs: dict[str, Any]) -> None:
        obs = self.s.observe()
        missing = [o.name for o in cap.outputs if o.name not in outputs]
        if missing:
            self._fail("OUTPUT_MISSING", None, f"outputs {missing}", "not extracted", obs)
        for c in cap.success:
            ok = self._checkpoint(obs, c) if c.kind != "text_present" else \
                any(re.search(c.pattern or "", l) for l in visible_lines(obs))
            if not ok:
                self._fail("SUCCESS_CHECK_FAILED", None, f"{c.kind} {c.pattern} {c.description}",
                           self._describe(obs), obs)
        self.ev.log("success_verified", checks=[c.description or c.kind for c in cap.success])

    def _finish_success(self, cap: Capability, outputs: dict[str, Any]) -> None:
        obs = self.s.observe()
        self.ev.screenshot(self.s, obs, "success")
        self.result.status = "success"
        self.result.outputs = outputs

    def _describe(self, obs: Observation) -> str:
        self.red.learn(obs)
        top = obs.top_window
        parts = [f"windows={[w.title for w in obs.windows]}", f"open={obs.containers()}"]
        if top and top.modal:
            parts.append("dialog_text=" + " ".join(n.name for n in top.nodes if n.role == "label" and n.name))
        status = [l for l in visible_lines(obs) if re.match(r"^\d{2}:\d{2}:\d{2}\s", l)]
        if status:
            parts.append(f"status={status[0]}")
        return self.red.text(" ".join(parts)) or ""

    def _fail(self, code: str, step: Step | None, expected: str, observed: str,
              obs: Observation | None = None, dismiss: str | None = None) -> None:
        evidence: dict[str, str] = {}
        if obs is not None:
            shot = self.ev.screenshot(self.s, obs, f"failure-{code}")
            if shot:
                evidence["screenshot"] = shot
            evidence["tree"] = self.ev.tree(obs, f"failure-{code}")
            if dismiss:
                self._dismiss(obs, dismiss)
        f = Failure(code=code, step_id=step and step.id, step_intent=step and step.intent,
                    expected=expected, observed=self.red.text(observed), evidence=evidence)
        raise _Stop(self.result.model_copy(update={"status": "failed", "failure": f}))
