"""Safety policy: an explicit allowlist plus risk classification, enforced before every action.

The same Policy object gates the LLM during discovery and the replay engine in production, so a
capability can never do at replay time what policy would not have let the agent do.

Risk classes:
  safe          navigation, reading, typing into a form (reversible until submitted)
  risky         state-changing but reversible or low impact (flagged in the log)
  irreversible  posts money, deletes, submits; requires an explicit approval
  blocked       never allowed (sign off, exit, admin menus)

Decision for irreversible: REQUIRE_APPROVAL. We chose approval over a hard block because the
point of the system is to do real work, and over flag-only because a posted transaction cannot be
taken back. Approval comes from a human operator (discovery, or replay without a grant) or from a
per-invocation grant by the calling agent for an APPROVED capability, which itself was human-reviewed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from .surface.base import Node


class Verdict(str, Enum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


@dataclass
class Decision:
    verdict: Verdict
    risk: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {"verdict": self.verdict.value, "risk": self.risk, "reason": self.reason}


@dataclass
class RiskRule:
    risk: str
    role: str | None = None
    name: re.Pattern[str] | None = None
    window: re.Pattern[str] | None = None
    container: re.Pattern[str] | None = None
    key: re.Pattern[str] | None = None

    def matches(self, op: str, node: Node | None, key: str | None) -> bool:
        if self.key is not None:
            return op == "key" and key is not None and bool(self.key.search(key))
        if node is None:
            return False
        if self.role and node.role != self.role:
            return False
        if self.name and not self.name.search(node.name or ""):
            return False
        if self.window and not self.window.search(node.window or ""):
            return False
        if self.container and not self.container.search(node.container or ""):
            return False
        return True


class Policy:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.name = cfg.get("name", "policy")
        self.allowed_ops = set(cfg.get("allowed_actions", []))
        self.allowed_windows = [re.compile(p) for p in cfg.get("allowed_windows", [".*"])]
        self.allowed_keys = {k.upper() for k in cfg.get("allowed_keys", [])}
        self.rules: list[RiskRule] = []
        for r in cfg.get("risk_rules", []):
            m = r.get("match", {})
            self.rules.append(RiskRule(
                risk=r["risk"], role=m.get("role"),
                name=re.compile(m["name"]) if "name" in m else None,
                window=re.compile(m["window"]) if "window" in m else None,
                container=re.compile(m["container"]) if "container" in m else None,
                key=re.compile(m["key"]) if "key" in m else None))

    @staticmethod
    def load(path: str | Path) -> "Policy":
        return Policy(yaml.safe_load(Path(path).read_text()))

    def classify(self, op: str, node: Node | None, key: str | None = None) -> str:
        for r in self.rules:
            if r.matches(op, node, key):
                return r.risk
        return "safe"

    def check(self, op: str, node: Node | None = None, key: str | None = None) -> Decision:
        if op not in self.allowed_ops:
            return Decision(Verdict.DENY, "blocked", f"action '{op}' is not in the allowlist")
        if op == "key" and key and key.upper() not in self.allowed_keys:
            return Decision(Verdict.DENY, "blocked", f"key '{key}' is not in the allowlist")
        if node is not None and not any(p.search(node.window or "") for p in self.allowed_windows):
            return Decision(Verdict.DENY, "blocked", f"window '{node.window}' is outside the allowlist")
        risk = self.classify(op, node, key)
        if risk == "blocked":
            return Decision(Verdict.DENY, risk, "target is on the blocked list")
        if risk == "irreversible":
            return Decision(Verdict.REQUIRE_APPROVAL, risk, "irreversible action needs explicit approval")
        return Decision(Verdict.ALLOW, risk, "allowed")
