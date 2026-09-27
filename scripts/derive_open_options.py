"""Derive "get open-account options" from the LLM-discovered open-sub-account artifact.

It reuses the recorded first three steps (open the Open Sub-Account screen, enter the member number,
click Load), then reads the two drop-downs instead of filling them:
  * account_types: the products THIS credit union offers (tenant-specific, read from the live app)
  * fund_from:     the member's own shares that can fund the new account
It is read-only, since nothing is posted. The assistant calls it first, so it only offers valid choices.

    python scripts/derive_open_options.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cua.agent.compile import load_capability, save_capability  # noqa: E402
from cua.artifact import Extraction, OutputSpec, Step  # noqa: E402

SOURCE = "corelink.account.open_share_sub_account"


def main() -> None:
    src = load_capability(SOURCE)
    by_param = {s.value.param: s for s in src.steps if s.value is not None and s.value.source == "param"}
    load_step = next(s for s in src.steps if s.action == "click" and s.target and s.target.description.startswith(
        "push button 'Load'"))
    head = src.steps[: src.steps.index(load_step) + 1]  # open screen, member number, Load
    cap = src.model_copy(deep=True)
    cap.id = "corelink.account.get_open_options"
    cap.version = "1.0.0"
    cap.status = "draft"
    cap.title = "List account types that can be opened, and the member's funding shares"
    cap.description = ("Open the Open Sub-Account screen for a member and read, without posting anything, which "
                       "account types this credit union offers and which of the member's shares can fund a new "
                       "account. Call this before opening an account so only valid choices are offered. Read-only.")
    cap.inputs = [p for p in src.inputs if p.name == "member_number"]
    cap.outputs = [
        OutputSpec(name="account_types", type="list", description="Account types offered by this credit union"),
        OutputSpec(name="fund_from", type="list", description="Member's shares that can fund it, e.g. 'S01 Share Savings'"),
    ]
    cap.steps = [s.model_copy(deep=True) for s in head] + [
        Step(id=f"s{len(head) + 1}", intent="Read the account types offered by this credit union.", action="extract",
             target=by_param["account_type"].target.model_copy(deep=True),
             extract=Extraction(output="account_types", read="options")),
        Step(id=f"s{len(head) + 2}", intent="Read the member's shares that can fund the new account.", action="extract",
             target=by_param["fund_from"].target.model_copy(deep=True),
             extract=Extraction(output="fund_from", read="options")),
    ]
    cap.success = []
    cap.risk = "read_only"
    cap.business_outcomes = [b for b in src.business_outcomes
                             if b.code in ("MEMBER_NOT_FOUND", "MEMBER_RESTRICTED", "MEMBER_CLOSED", "PERMISSION_DENIED")]
    cap.provenance.notes = list(src.provenance.notes) + [
        f"Derived from {src.id}@{src.version} (discovery run {src.provenance.discovery_run}): steps up to Load are "
        "reused unchanged; the two drop-downs are read as outputs instead of being filled. Nothing is posted."]
    cap.provenance.reviewed_by = None
    print(save_capability(cap))


if __name__ == "__main__":
    main()
