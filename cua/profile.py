"""App profiles: vendor-product-level configuration shared by all tenants running that product."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .artifact import OutcomeRule, Step

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class TenantOverride:
    tenant: str
    product_version: str | None = None
    label_aliases: dict[str, str] = field(default_factory=dict)
    name_aliases: dict[str, str] = field(default_factory=dict)
    extra_outcome_rules: list[OutcomeRule] = field(default_factory=list)


@dataclass
class AppProfile:
    id: str
    product: str
    surface: str
    main_window: str
    sign_on_window: str
    pii_labels: list[str]
    sign_on: list[Step]
    outcome_rules: list[OutcomeRule]
    tenants: dict[str, TenantOverride]

    @staticmethod
    def load(profile_id: str) -> "AppProfile":
        d: dict[str, Any] = yaml.safe_load((ROOT / "configs" / "apps" / f"{profile_id}.yaml").read_text())
        tenants = {}
        for name, t in (d.get("tenants") or {}).items():
            t = t or {}
            tenants[name] = TenantOverride(
                tenant=name, product_version=t.get("product_version"),
                label_aliases=t.get("label_aliases", {}), name_aliases=t.get("name_aliases", {}),
                extra_outcome_rules=[OutcomeRule(**r) for r in t.get("extra_outcome_rules", [])])
        return AppProfile(
            id=d["id"], product=d["product"], surface=d["surface"], main_window=d["main_window"],
            sign_on_window=d["sign_on_window"], pii_labels=d.get("pii_labels", []),
            sign_on=[Step(**s) for s in d.get("sign_on", [])],
            outcome_rules=[OutcomeRule(**r) for r in d.get("outcome_rules", [])], tenants=tenants)

    def tenant(self, name: str | None) -> TenantOverride:
        return self.tenants.get(name or "", TenantOverride(tenant=name or "default"))

    def rules_for(self, tenant: str | None, capability_rules: list[OutcomeRule], inherit: bool) -> list[OutcomeRule]:
        """Capability rules first (most specific), then tenant extras, then product-wide rules."""
        rules = list(capability_rules) + list(self.tenant(tenant).extra_outcome_rules)
        if inherit:
            rules += self.outcome_rules
        return rules
