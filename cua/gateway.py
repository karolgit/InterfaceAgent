"""Capability gateway: the agent-facing surface over saved artifacts (stretch goal: catalog + invoke).

* catalog(): capabilities an agent may call, as typed tool definitions (contract only, no flow).
* Parameters bound by the caller's authenticated context (e.g. the member's own number) are REMOVED
  from the tool schema, so the model cannot choose whose account to act on.
* Irreversible capabilities never run on the model's say-so: invoke() returns a pending confirmation
  that only the end user (or an operator) can grant through a separate channel.
* One live app session, serialized with a lock (a desktop session is single-threaded by nature).
"""
from __future__ import annotations

import threading
import uuid
from pathlib import Path
from typing import Any

from .agent.compile import load_capability
from .artifact import Capability
from .runtime import FAULTS, run_replay, write_faults
from .surface.swing import SwingSurface, launch_corelink

ROOT = Path(__file__).resolve().parents[1]


def tool_name(cap_id: str) -> str:
    return cap_id.replace(".", "__")


class CapabilityGateway:
    def __init__(self, tenant: str = "heritage", allow_draft: bool = False, port: int = 8741):
        self.tenant = tenant
        self.allow_draft = allow_draft
        self.port = port
        self.lock = threading.Lock()
        self.proc = None
        self.surface: SwingSurface | None = None
        self.pending: dict[str, dict[str, Any]] = {}

    # -- catalog
    def capabilities(self) -> list[Capability]:
        caps = [load_capability(str(p)) for p in sorted((ROOT / "capabilities").glob("*.json"))]
        return [c for c in caps if c.status == "approved" or (self.allow_draft and c.status == "draft")]

    def tools(self, bound: set[str]) -> list[dict[str, Any]]:
        out = []
        for c in self.capabilities():
            props, req = {}, []
            for p in c.inputs:
                if p.name in bound:
                    continue
                sch: dict[str, Any] = {"type": "string", "description": p.description}
                if p.enum:
                    sch["enum"] = p.enum
                if p.pattern:
                    sch["pattern"] = p.pattern
                props[p.name] = sch
                if p.required:
                    req.append(p.name)
            outcomes = ", ".join(b.code for b in c.business_outcomes)
            desc = (f"{c.title}. {c.description} Returns: "
                    + ", ".join(f"{o.name} ({o.type})" for o in c.outputs)
                    + f". Possible business outcomes: {outcomes}.")
            if c.risk == "irreversible":
                desc += (" IRREVERSIBLE, but safe to call: calling it does NOT execute anything. It returns "
                         "needs_confirmation and the app shows the user a Confirm button, which is the only "
                         "confirmation step. Call it as soon as you have all inputs; don't ask for confirmation "
                         "in chat first. You cannot confirm on the user's behalf.")
            out.append({"name": tool_name(c.id), "description": desc,
                        "input_schema": {"type": "object", "properties": props, "required": req,
                                         "additionalProperties": False}})
        return out

    def by_tool(self, name: str) -> Capability:
        for c in self.capabilities():
            if tool_name(c.id) == name:
                return c
        raise KeyError(name)

    # -- invocation
    def _ensure_app(self) -> SwingSurface:
        if self.surface is not None and self.surface.healthy():
            return self.surface
        write_faults({})
        self.proc, self.surface = launch_corelink(tenant=self.tenant, port=self.port, faults_file=FAULTS,
                                                  log_file=ROOT / "runs" / "corelink-gateway.log")
        return self.surface

    def invoke(self, tool: str, args: dict[str, Any], bound: dict[str, Any], requested_by: str) -> dict[str, Any]:
        cap = self.by_tool(tool)
        params = {**args, **bound}  # bound values always win over anything the model passed
        if cap.risk == "irreversible":
            pid = uuid.uuid4().hex[:8]
            self.pending[pid] = {"tool": tool, "params": params, "requested_by": requested_by, "cap": cap.id}
            shown = {k: v for k, v in args.items()}
            return {"status": "needs_confirmation", "pending_id": pid, "capability": cap.title,
                    "details": shown,
                    "message": "Waiting for the member to confirm in the app. Tell them what will happen and ask "
                               "them to press Confirm."}
        return self._run(cap, params, approve=False)

    def confirm(self, pid: str) -> dict[str, Any]:
        p = self.pending.pop(pid, None)
        if p is None:
            return {"status": "rejected", "failure": {"code": "NO_SUCH_PENDING_ACTION"}}
        return self._run(load_capability(p["cap"]), p["params"], approve=True)

    def cancel(self, pid: str) -> None:
        self.pending.pop(pid, None)

    def _run(self, cap: Capability, params: dict[str, Any], approve: bool) -> dict[str, Any]:
        with self.lock:
            s = self._ensure_app()
            res = run_replay(cap, params, s, self.tenant, approve_irreversible=approve,
                             allow_draft=self.allow_draft, run_prefix="gateway", handoff_timeout_s=300)
        return res.model_dump(exclude_none=True, include={"status", "outputs", "outcome", "failure", "run_id",
                                                          "recoveries", "handoffs"})

    def shutdown(self) -> None:
        if self.proc:
            self.proc.kill()
