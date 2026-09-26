"""Derive the generic "get share balance" capability from the LLM-discovered savings-balance artifact.

The recorded flow is the same for every share type: open Member Inquiry, enter the member number,
run the inquiry, read one row of the accounts table. Only the row filter differs. Instead of a new
discovery per row, this parameterizes the filter (row_match.contains -> contains_param) and adds a
typed `share_type` input. Every other step, locator, and checkpoint is inherited unchanged.
Provenance records the derivation, and the result starts as a draft for human review.

    python scripts/derive_share_balance.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cua.agent.compile import load_capability, save_capability  # noqa: E402
from cua.artifact import OutcomeRef, OutputSpec, ParamSpec  # noqa: E402

SOURCE = "corelink.member.get_share_savings_balance"
SHARE_TYPES = ["Share Savings", "Share Draft Checking", "Money Market", "Share Certificate",
               "Christmas Club", "Holiday Club", "Youth Savings"]


def main() -> None:
    src = load_capability(SOURCE)
    cap = src.model_copy(deep=True)
    cap.id = "corelink.member.get_share_balance"
    cap.version = "1.0.0"
    cap.status = "draft"
    cap.title = "Get member share balance by share type"
    cap.description = ("Look up a member by member number in CoreLink Member Inquiry and return the current balance "
                       "of one of the member's shares, chosen by share_type (for example Share Savings or "
                       "Share Draft Checking). Read-only.")
    cap.inputs = [p for p in src.inputs] + [ParamSpec(
        name="share_type", type="enum", enum=SHARE_TYPES, required=False, default="Share Savings",
        description="Which share to read, matched against the Description column")]
    cap.outputs = [OutputSpec(name="balance", type="currency", sensitive=True,
                              description="Current balance of the requested share in USD, as a decimal string")]
    ext = [s for s in cap.steps if s.action == "extract"]
    assert len(ext) == 1 and ext[0].extract.read == "table_cell", "unexpected source flow"
    ext[0].extract.output = "balance"
    ext[0].extract.row_match.contains = None
    ext[0].extract.row_match.contains_param = "share_type"
    ext[0].extract.no_match_outcome = "SHARE_NOT_FOUND"
    ext[0].intent = "Capture the balance from the row whose Description matches share_type."
    cap.business_outcomes = list(src.business_outcomes) + [
        OutcomeRef(code="SHARE_NOT_FOUND", description="The member has no share of the requested type.")]
    cap.provenance.notes = list(src.provenance.notes) + [
        f"Derived from {src.id}@{src.version} (discovery run {src.provenance.discovery_run}): the extraction row "
        "filter was parameterized as share_type; all other steps, locators, and checkpoints are unchanged."]
    cap.provenance.reviewed_by = None
    print(save_capability(cap))


if __name__ == "__main__":
    main()
