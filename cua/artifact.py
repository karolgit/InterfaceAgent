"""Capability artifact schema (v1).

A capability is what an AI agent calls. The artifact has two audiences:
  * the calling agent reads the CONTRACT: id, description, inputs, outputs, outcomes, risk
  * the replay engine and a human reviewer read the FLOW: steps, locators, checkpoints

Design choices:
  * Contract first, flow second: a caller never needs to parse steps to know what it gets back.
  * Every target carries several independent locator strategies, ranked most to least robust,
    each recorded with whether it was unique when discovered. Replay tries them in order and
    reports which one matched, so drift shows up as "fell back to strategy 3" before it breaks.
  * Values come from a typed source (literal / param / secret). Secrets are references only;
    the artifact never contains credentials or example PII.
  * Checkpoints are explicit after each step, not implied by "the click didn't throw".
  * Outcomes (business results, recoverable conditions, hard failures) are declared data, mostly
    inherited from the app profile (vendor product level), so every capability on the same product
    gets the same error taxonomy for free.
  * Scope is product + version range + tenant overrides, not a tenant, so one recording serves
    every institution running that vendor product.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field

SCHEMA_VERSION = "cua.capability/v1"


# ----------------------------------------------------------------------------- contract

class ParamSpec(BaseModel):
    name: str
    type: Literal["string", "integer", "decimal", "enum"] = "string"
    description: str = ""
    pattern: str | None = None
    enum: list[str] | None = None
    required: bool = True
    sensitive: bool = False  # never logged verbatim; registered with the redactor


class OutputSpec(BaseModel):
    name: str
    type: Literal["string", "currency", "integer", "decimal", "list"] = "string"
    description: str = ""
    sensitive: bool = False  # returned to the caller, fingerprinted in logs


class OutcomeRef(BaseModel):
    """A business outcome the caller should expect and handle (documented in the contract)."""
    code: str
    description: str = ""


# ----------------------------------------------------------------------------- locators

class Scope(BaseModel):
    """Where to look. 'main' is the primary app window; dialogs match by title regex."""
    window: str = "main"
    container: str | None = None  # internal frame / panel title regex


class A11yLocator(BaseModel):
    kind: Literal["a11y"] = "a11y"
    role: str
    name: str  # exact accessible name, or regex if it starts with ^
    unique_at_record: bool = True


class LabelLocator(BaseModel):
    kind: Literal["label"] = "label"
    role: str
    label: str
    unique_at_record: bool = True


class GroupLocator(BaseModel):
    """Nth control of a role inside a named group/panel, e.g. the table under 'Share Accounts'."""
    kind: Literal["group"] = "group"
    role: str
    group: str
    index: int = 0
    unique_at_record: bool = True


class PathLocator(BaseModel):
    kind: Literal["path"] = "path"
    role: str
    path: str  # index path relative to the scope's container (or window)
    unique_at_record: bool = True


class CoordLocator(BaseModel):
    kind: Literal["coords"] = "coords"
    rel_x: float  # fraction of the scope's width/height; last resort
    rel_y: float
    unique_at_record: bool = True


Locator = Annotated[Union[A11yLocator, LabelLocator, GroupLocator, PathLocator, CoordLocator], Field(discriminator="kind")]


class Target(BaseModel):
    description: str
    scope: Scope = Scope()
    strategies: list[Locator]


# ----------------------------------------------------------------------------- values / extraction

class LiteralValue(BaseModel):
    source: Literal["literal"] = "literal"
    value: str


class ParamValue(BaseModel):
    source: Literal["param"] = "param"
    param: str


class SecretValue(BaseModel):
    source: Literal["secret"] = "secret"
    secret: str  # key into the secret store; value never appears in artifacts or logs


Value = Annotated[Union[LiteralValue, ParamValue, SecretValue], Field(discriminator="source")]


class RowMatch(BaseModel):
    column: str
    contains: str


class Extraction(BaseModel):
    output: str
    read: Literal["value", "name", "table_cell"] = "value"
    row_match: RowMatch | None = None
    column: str | None = None
    parse: Literal["text", "currency", "integer"] = "text"
    pattern: str | None = None  # regex with one capture group applied to the raw text first


# ----------------------------------------------------------------------------- checkpoints

class Checkpoint(BaseModel):
    """A condition that must hold after a step. All checkpoints of a step must hold."""
    kind: Literal["container_present", "window_present", "text_present", "element_value", "element_present"]
    pattern: str | None = None  # regex for titles / text / values
    target: Target | None = None  # element checks default to the step's own target
    description: str = ""


# ----------------------------------------------------------------------------- steps

class Step(BaseModel):
    id: str
    intent: str  # human-readable purpose, shown to reviewers and in failure reports
    action: Literal["click", "set_text", "select", "key", "extract", "wait_for"]
    target: Target | None = None
    value: Value | None = None
    key: str | None = None
    extract: Extraction | None = None
    risk: Literal["safe", "risky", "irreversible"] = "safe"
    expect: list[Checkpoint] = []
    timeout_ms: int = 8000


# ----------------------------------------------------------------------------- outcome rules

class Recovery(BaseModel):
    kind: Literal["dismiss", "wait", "resign_on_and_restart", "none"] = "none"
    button: str | None = None  # for dismiss
    max_attempts: int = 1


class OutcomeRule(BaseModel):
    """Maps an observed UI condition to a classified result. Usually declared in the app profile."""
    id: str
    when_text: str | None = None  # regex over visible text (only text that newly appeared)
    when_dialog: str | None = None  # regex over modal dialog title
    kind: Literal["business_outcome", "recoverable", "hard_failure", "escalate"]
    code: str
    message: str = ""
    recovery: Recovery = Recovery()
    capture_text: bool = True  # include the UI's own message in the result


class AppRef(BaseModel):
    product: str
    versions: str = "*"  # semver-ish range this capability was validated on
    surface: str = "java-swing"
    profile: str  # app profile id holding sign-on, shared outcome rules, tenant overrides


class Provenance(BaseModel):
    discovered_by: str  # "llm:<model>" or "human"
    discovery_run: str | None = None
    goal: str = ""
    tenant: str | None = None
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    reviewed_by: str | None = None
    notes: list[str] = []


class Capability(BaseModel):
    schema_version: Literal["cua.capability/v1"] = SCHEMA_VERSION
    id: str
    version: str = "1.0.0"
    status: Literal["draft", "approved", "deprecated"] = "draft"
    title: str
    description: str
    app: AppRef
    inputs: list[ParamSpec] = []
    outputs: list[OutputSpec] = []
    business_outcomes: list[OutcomeRef] = []
    risk: Literal["read_only", "state_changing", "irreversible"] = "read_only"
    preconditions: list[str] = ["signed_on", "no_modal_dialog"]
    steps: list[Step]
    success: list[Checkpoint] = []
    outcome_rules: list[OutcomeRule] = []  # capability-specific, evaluated before profile rules
    inherit_outcome_rules: bool = True
    provenance: Provenance

    def input_spec(self, name: str) -> ParamSpec | None:
        return next((p for p in self.inputs if p.name == name), None)

    def to_json(self) -> str:
        return self.model_dump_json(indent=2, exclude_none=True)

    def summary(self) -> dict[str, Any]:
        """What the calling agent sees in the catalog: contract only, no flow."""
        return {
            "id": self.id, "version": self.version, "status": self.status, "title": self.title,
            "description": self.description, "risk": self.risk,
            "inputs": [p.model_dump(exclude_none=True) for p in self.inputs],
            "outputs": [o.model_dump(exclude_none=True) for o in self.outputs],
            "business_outcomes": [b.model_dump() for b in self.business_outcomes],
        }


# ----------------------------------------------------------------------------- run result contract

class Failure(BaseModel):
    code: str
    step_id: str | None = None
    step_intent: str | None = None
    expected: str | None = None
    observed: str | None = None
    evidence: dict[str, str] = {}


class RunResult(BaseModel):
    """What replay returns to the caller. Exactly one of outputs / outcome / failure is meaningful."""
    run_id: str
    capability: str
    status: Literal["success", "business_outcome", "failed", "rejected"]
    outputs: dict[str, Any] = {}
    outcome: dict[str, Any] | None = None  # {code, message} for business outcomes
    failure: Failure | None = None
    recoveries: list[dict[str, Any]] = []
    handoffs: list[dict[str, Any]] = []
    locator_fallbacks: list[dict[str, Any]] = []
    duration_ms: int = 0
    evidence_dir: str | None = None
