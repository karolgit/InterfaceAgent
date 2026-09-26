"""Unit tests for the load-bearing pure logic. No app, no LLM: run with `pytest -q`."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from cua.agent.compile import generalize, load_capability
from cua.artifact import (A11yLocator, Capability, CoordLocator, GroupLocator, LabelLocator, PathLocator, Scope,
                          Target)
from cua.locate import build_target, resolve
from cua.policy import Policy, Verdict
from cua.profile import AppProfile
from cua.redact import Redactor, fingerprint
from cua.replay import parse_value, visible_lines
from cua.surface.base import Observation

ROOT = Path(__file__).resolve().parents[1]
MAIN = r"^CoreLink \d+\.\d+.* - .+$"


def obs_fixture(member_label: str = "Member #:", button: str = "Inquire") -> Observation:
    """A hand-written accessibility snapshot shaped like the bridge's /tree output."""
    return Observation.from_json({"windows": [{
        "index": 0, "title": "CoreLink 7.4.1 - Heritage Federal Credit Union", "kind": "frame", "modal": False,
        "bounds": [0, 0, 1000, 700],
        "nodes": [
            {"ref": "w0", "role": "frame", "name": "CoreLink", "depth": 0, "states": ["enabled"]},
            {"ref": "w0.1", "role": "internal frame", "name": "Member Inquiry", "container": "Member Inquiry",
             "depth": 1, "bounds": [20, 100, 600, 400], "states": ["enabled"]},
            {"ref": "w0.1.0", "role": "label", "name": member_label, "container": "Member Inquiry", "depth": 2,
             "bounds": [30, 130, 60, 16], "states": ["enabled"]},
            {"ref": "w0.1.1", "role": "text", "name": "", "value": "", "container": "Member Inquiry", "depth": 2,
             "bounds": [100, 128, 120, 20], "states": ["enabled", "editable"]},
            {"ref": "w0.1.2", "role": "push button", "name": button, "container": "Member Inquiry", "depth": 2,
             "bounds": [230, 126, 70, 24], "states": ["enabled"], "actionable": True},
            {"ref": "w0.1.3", "role": "panel", "name": "Member Information", "container": "Member Inquiry",
             "depth": 2, "bounds": [30, 160, 560, 100], "states": ["enabled"]},
            {"ref": "w0.1.3.0", "role": "label", "name": "SSN:", "container": "Member Inquiry", "depth": 3,
             "bounds": [40, 180, 40, 16], "states": ["enabled"]},
            {"ref": "w0.1.3.1", "role": "label", "name": "900-12-3456", "container": "Member Inquiry", "depth": 3,
             "bounds": [200, 180, 90, 16], "states": ["enabled"]},
            {"ref": "w0.1.4", "role": "scroll pane", "name": "Share Accounts", "container": "Member Inquiry",
             "depth": 2, "bounds": [30, 270, 560, 120], "states": ["enabled"]},
            {"ref": "w0.1.4.0", "role": "table", "name": "", "container": "Member Inquiry", "depth": 3,
             "bounds": [32, 290, 556, 90], "states": ["enabled"], "columns": ["Suffix", "Description", "Balance"],
             "rows": [["S01", "Share Savings", "$4,210.55"], ["S10", "Share Draft Checking", "$1,893.20"]]},
            {"ref": "w0.9", "role": "label", "name": "01:02:03  INQUIRY COMPLETE - 2 SHARE(S)", "depth": 1,
             "bounds": [0, 680, 900, 18], "states": ["enabled"]},
        ]}]})


# ---------------------------------------------------------------- perception
def test_label_inferred_from_geometry_when_not_linked():
    o = obs_fixture()
    assert o.node("w0.1.1").label == "Member #:"


def test_group_inferred_from_named_ancestor_panel():
    assert obs_fixture().node("w0.1.4.0").group == "Share Accounts"


# ---------------------------------------------------------------- locators
def test_build_target_ranks_unique_strategies_and_resolves_back():
    o = obs_fixture()
    t = build_target(o, o.node("w0.1.1"), MAIN, "member number field")
    kinds = [s.kind for s in t.strategies]
    assert kinds[0] == "label" and "coords" in kinds and "path" in kinds
    assert all(s.unique_at_record for s in t.strategies)
    r = resolve(o, t, MAIN)
    assert r.node.ref == "w0.1.1" and r.strategy_index == 0


def test_tenant_alias_resolves_renamed_label_and_button():
    other = obs_fixture(member_label="Account No.:", button="Search")
    field = Target(description="f", scope=Scope(container="^Member Inquiry$"),
                   strategies=[LabelLocator(role="text", label="Member #:")])
    btn = Target(description="b", scope=Scope(container="^Member Inquiry$"),
                 strategies=[A11yLocator(role="push button", name="Inquire")])
    assert resolve(other, field, MAIN).node is None  # without override it must NOT guess
    r1 = resolve(other, field, MAIN, label_aliases={"Member #:": "Account No.:"})
    r2 = resolve(other, btn, MAIN, name_aliases={"Inquire": "Search"})
    assert r1.node.ref == "w0.1.1" and r2.node.ref == "w0.1.2"


def test_fallback_to_later_strategy_is_reported():
    o = obs_fixture(button="Lookup")  # drifted button text
    t = Target(description="b", scope=Scope(container="^Member Inquiry$"), strategies=[
        A11yLocator(role="push button", name="Inquire"), PathLocator(role="push button", path="2")])
    r = resolve(o, t, MAIN)
    assert r.node.ref == "w0.1.2" and r.strategy_index == 1 and "0 matches" in r.notes[0]


def test_group_and_coordinate_locators():
    o = obs_fixture()
    g = Target(description="t", scope=Scope(container="^Member Inquiry$"),
               strategies=[GroupLocator(role="table", group="Share Accounts")])
    assert resolve(o, g, MAIN).node.ref == "w0.1.4.0"
    c = Target(description="b", scope=Scope(container="^Member Inquiry$"),
               strategies=[CoordLocator(rel_x=(265 - 20) / 600, rel_y=(138 - 100) / 400)])
    assert resolve(o, c, MAIN).node.ref == "w0.1.2"


def test_missing_container_is_explained():
    o = obs_fixture()
    t = Target(description="x", scope=Scope(container="^Open Sub-Account"),
               strategies=[A11yLocator(role="push button", name="Load")])
    r = resolve(o, t, MAIN)
    assert r.node is None and "not open" in r.notes[0]


# ---------------------------------------------------------------- policy
@pytest.fixture
def policy() -> Policy:
    return Policy.load(ROOT / "configs" / "policies" / "corelink.yaml")


def test_policy_blocks_unlisted_action_and_key(policy):
    assert policy.check("drag").verdict == Verdict.DENY
    assert policy.check("key", key="F12").verdict == Verdict.DENY
    assert policy.check("key", key="F2").verdict == Verdict.ALLOW


def test_policy_requires_approval_for_posting(policy):
    o = Observation.from_json({"windows": [{"index": 0, "title": "CoreLink 7.4.1 - X", "kind": "frame",
                                            "modal": False, "bounds": [0, 0, 1, 1], "nodes": [
        {"ref": "w0.1", "role": "push button", "name": "Post Transaction"},
        {"ref": "w0.2", "role": "menu item", "name": "Exit"},
        {"ref": "w0.3", "role": "push button", "name": "Review >>"}]}]})
    assert policy.check("click", o.node("w0.1")).verdict == Verdict.REQUIRE_APPROVAL
    assert policy.check("click", o.node("w0.2")).verdict == Verdict.DENY
    d = policy.check("click", o.node("w0.3"))
    assert d.verdict == Verdict.ALLOW and d.risk == "risky"


def test_policy_denies_windows_outside_allowlist(policy):
    o = Observation.from_json({"windows": [{"index": 0, "title": "Notepad", "kind": "frame", "modal": False,
                                            "bounds": [0, 0, 1, 1],
                                            "nodes": [{"ref": "w0.1", "role": "push button", "name": "Save"}]}]})
    assert policy.check("click", o.node("w0.1")).verdict == Verdict.DENY


# ---------------------------------------------------------------- redaction
def test_redactor_masks_patterns_literals_and_labeled_values():
    r = Redactor(pii_labels=["SSN", "Name"])
    r.register("12345", "member_number")
    s = r.text("member 12345 ssn 900-12-3456 dob 03/14/1978 bal $4,210.55")
    assert "12345" not in s and "900-12-3456" not in s and "03/14/1978" not in s and "4,210" not in s
    o = obs_fixture()
    assert "w0.1.3.1" in r.sensitive_nodes(o)  # value right of 'SSN:' label
    assert r.obj({"password": "hunter2", "x": ["900-12-3456"]}) == {"password": "[SECRET]", "x": ["[SSN]"]}


def test_fingerprint_is_stable_and_not_reversible_looking():
    assert fingerprint("4210.55") == fingerprint("4210.55") and "4210" not in fingerprint("4210.55")


# ---------------------------------------------------------------- replay helpers + artifact
def test_parse_currency_and_generalize():
    assert parse_value("$4,210.55", "currency") == "4210.55"
    assert parse_value("($12.00)", "currency") == "-12.00"
    with pytest.raises(ValueError):
        parse_value("n/a", "currency")
    assert generalize("01:02:03  INQUIRY COMPLETE - 2 SHARE(S)") == r"INQUIRY COMPLETE - \d+ SHARE\(S\)"


def test_visible_lines_exclude_typed_input():
    o = obs_fixture()
    o.node("w0.1.1").value = "MBR NOT ON FILE"  # something WE typed must never trigger a rule
    assert "MBR NOT ON FILE" not in visible_lines(o)
    assert "S01 | Share Savings | $4,210.55" in visible_lines(o)


def test_profile_rules_order_capability_then_tenant_then_product():
    p = AppProfile.load("corelink")
    rules = p.rules_for("lakeshore", [], True)
    assert rules[0].id == "disclosure_ack" and any(r.id == "member_not_found" for r in rules)


def test_saved_artifacts_validate_and_contain_no_sensitive_literals():
    for path in (ROOT / "capabilities").glob("*.json"):
        cap = load_capability(str(path))
        assert isinstance(cap, Capability) and cap.steps
        raw = path.read_text()
        for bad in ("12345", "23456", "900-", "4210", "demo123", "teller01"):
            assert bad not in raw, f"{bad} leaked into {path.name}"
        assert json.loads(raw)["schema_version"] == "cua.capability/v1"


# ---------------------------------------------------------------- Gemini planner (offline, faked API)
def test_gemini_planner_round_trip(tmp_path, monkeypatch):
    from google.genai import types
    from cua.agent.discover import GeminiPlanner, Task, ToolCall
    from cua.evidence import Evidence

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    ev = Evidence("t", Redactor(), base=tmp_path)
    planner = GeminiPlanner(ev, model="gemini-test")
    sent = []

    def fake_generate(model, contents, config):
        sent.append(list(contents))
        return types.GenerateContentResponse(
            candidates=[types.Candidate(content=types.Content(role="model", parts=[
                types.Part(text="Open the inquiry screen first.", thought=True),
                types.Part(function_call=types.FunctionCall(id="c1", name="click",
                                                            args={"ref": "w0.1", "why": "open inquiry"}))]))],
            usage_metadata=types.GenerateContentResponseUsageMetadata(prompt_token_count=100,
                                                                      candidates_token_count=10))

    monkeypatch.setattr(planner.client.models, "generate_content", fake_generate)
    task = Task(capability_id="x", title="t", description="d", app_profile="corelink", goal="g",
                inputs=[], outputs=[], business_outcomes=[], example_inputs={})
    planner.start(task)
    calls = planner.decide("outline", b"\x89PNG", [])
    assert calls == [ToolCall("c1", "click", {"ref": "w0.1", "why": "open inquiry"})]
    calls2 = planner.decide("outline 2", b"\x89PNG", [(calls[0], "Done.", False)])
    last_user = sent[-1][-1]
    assert last_user.role == "user" and last_user.parts[0].function_response.name == "click"
    assert last_user.parts[0].function_response.response == {"result": "Done."}
    assert planner.usage["input_tokens"] == 200 and calls2[0].name == "click"
    assert "model_thinking_summary" in (tmp_path / "t" / "log.jsonl").read_text()
