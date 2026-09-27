# REPORT

## 1. Architecture

**Target choice.** The proxy is CoreLink, a Java Swing desktop client I built to look like a legacy
core-banking teller app. It uses an MDI desktop, modal option dialogs, upper-case status-bar errors, and no
test IDs. On one form the labels are not wired to their fields. I chose a desktop app over a web demo because
the brief says the common case has no clean DOM. A desktop app forces the perception and locator design to
cope with that. I built it myself because every runtime condition the brief lists has to be reproducible on
demand. Faults are injected through a properties file that the app re-reads on every action.

**Layers.** Each layer talks only to the one below it.

| Layer | Responsibility | Code |
|---|---|---|
| Surface driver | Perceive (tree, screenshot), act (click, set text, select, key, close), control lock, input recorder | `bridge/` (Java), `cua/surface/` |
| Policy + redaction | Allow, deny, or require approval for every action. Mask PII before anything leaves memory | `cua/policy.py`, `cua/redact.py` |
| Discovery | LLM observe, decide, act loop that records a trace | `cua/agent/discover.py` |
| Compiler | Trace to capability artifact, decoupled from the transcript | `cua/agent/compile.py` |
| Replay | Deterministic execution and outcome classification | `cua/replay.py` |
| Handoff | Pause, cede control, record the human, resume | `cua/handoff.py`, `cua/console.py` |
| Gateway | Catalog of approved capabilities as typed tools, with an invoke path | `cua/gateway.py`, `portal/` |

**Key decisions and trade-offs.**
- **An in-process accessibility bridge, not screenshots plus coordinates.** A `-javaagent` jar attaches to
  the unmodified app and reads `javax.accessibility`. That is the same role, name, state, and bounds data the
  Windows Java Access Bridge exposes to screen readers. Accessibility data is available for desktop apps
  (JAB, UI Automation) and for legacy web apps, so the design does not depend on a DOM. It is also far more
  stable than pixels. Actions go through accessibility actions rather than OS mouse events. They work while
  the desktop is locked and never collide with a person's input. The trade-off is that a vendor app might
  block agent injection. The fallback is the external Access Bridge, or UI Automation for non-Java apps,
  with the same Surface interface above it.
- **The model points, the harness records.** The model acts only on element refs from the current
  outline. The compiler turns each targeted element into ranked locators computed from the live tree. The
  model never writes selectors, which keeps its hallucinations out of the artifact.
- **Model is pluggable.** The planner interface has two real implementations. The default is Claude Opus 5,
  through the official SDK, with adaptive thinking. The other is Gemini, through google-genai. Both get the
  same redacted inputs and tool schema, both use a manual loop with one action per turn, and both log their
  reasoning summaries as evidence. Discovery is rare, so I favour the strongest model over cost there.
  Replay never calls a model. The planner used for each run is recorded in the artifact's provenance.
- **One process, file-based intervention queue.** A desktop session is single-threaded by nature. The
  gateway serializes invocations with a lock. Queues and workers are deliberately not built; see Cuts.

## 2. Artifact schema

The schema is `cua.capability/v1`. It is Pydantic, exported as JSON Schema in `schemas/`. An example is
`capabilities/corelink.member.get_share_savings_balance.v1.0.0.json`. The artifact is split into a
**contract** and a **flow** because it has two readers.

**Contract** is what a calling agent reads:
- `id`, `version` (semver), and `status`, which is draft, approved, or deprecated.
- `title` and `description`.
- `inputs`: typed `ParamSpec` with pattern or enum and a `sensitive` flag.
- `outputs`: typed `OutputSpec` such as `currency`, with a `sensitive` flag.
- `business_outcomes`: the codes the caller must handle, such as `MEMBER_NOT_FOUND`.
- `risk`: read_only, state_changing, or irreversible.

`cua catalog` prints only this part.

**Flow** is what replay and a reviewer read:
- `steps`: each step has an `intent` in plain English, an `action`, a `target`, a `value`, a `risk`, a
  list of `expect` checkpoints, and a `timeout_ms`.
- `target = scope + strategies[]`. The scope names the window ("main" or a dialog-title regex) and the
  work-window container. Strategies are ranked and independent, and each records `unique_at_record`:
  1. `a11y`: role plus accessible name. This is the most semantic, and it is what a screen reader
     announces.
  2. `label`: role plus visible label text, taken from the label relation or inferred from geometry
     (the nearest label on the same row, to the left). Legacy forms rarely wire labels, and a human finds
     the field this way too.
  3. `group`: the nth control of a role inside a named panel, for example the table under "Share Accounts".
  4. `path`: index path within the container. This is exact but breaks on layout change.
  5. `coords`: a relative point in the container, the last resort. It also carries over to
     screenshot-only surfaces.

  Replay reports which strategy matched. A fallback to strategy 3 shows drift before it becomes a
  failure.
- `value`: `literal`, `param`, or `secret`. A secret is a name only. Credentials and example PII never
  appear in the artifact, and a unit test enforces that.
- `extract`: value, name, or table cell (row match plus column), then a capture regex, then a parse type.
- `success`: final checkpoints.
- `outcome_rules`: capability-specific rules. Product-wide rules are inherited from the app profile.
- `provenance`: discovering model, run id, goal, tenant, reviewer, and notes. Notes flag any step where a
  human intervened during discovery.

**Why this shape.** A capability is a function signature plus a reviewable procedure. Putting the contract
first lets an agent choose a capability without parsing steps. Per-step `intent` and checkpoints let a
reviewer approve it without watching a video. Declaring outcomes as data, mostly at the product level,
means a new capability gets the full error taxonomy without new code.

## 3. Determinism & error handling

**Determinism.**
- **Fixed start state.** Replay signs on if needed through the vendor-profile routine and closes every open
  work window before the first step.
- **Ranked locators.** Each strategy must resolve to exactly one element. An ambiguous match falls through
  to the next strategy.
- **No sleeps.** Each step waits for its checkpoints, bounded by `timeout_ms`:
  - `container_present` and `window_present` for new windows and frames;
  - `text_present` for status lines, generalized as `INQUIRY COMPLETE - \d+ SHARE\(S\)`;
  - `element_value` for typed fields.

  Text checkpoints count only text that is new since the action, so a stale status line can't satisfy one.
  The mock stamps status lines with a time, as many legacy cores do.
- **Settle detection.** The UI counts as settled when the tree fingerprint stops changing.
- **Input contract.** Inputs are validated against the contract before the UI is touched. A bad input is
  `rejected / INVALID_INPUT`, not a UI failure.

**Error taxonomy.** While resolving a target or waiting on a checkpoint, the engine evaluates outcome rules
against modal dialogs and newly appeared text. Text we typed ourselves is excluded. First match wins, in
this order: capability rules, then tenant extras, then product rules.

| Class | Result `status` | Examples in CoreLink | Handling |
|---|---|---|---|
| Business outcome | `business_outcome` + `code` | `MEMBER_NOT_FOUND`, `MEMBER_RESTRICTED`, `MEMBER_CLOSED`, `VALIDATION_ERROR`, `PERMISSION_DENIED`, `OPERATOR_DECLINED`, `SHARE_NOT_FOUND`, `INVALID_OPTION` (with the valid choices) | Stop, return the code, the UI's own message, and a screenshot. This is not an error. |
| Recoverable | `success` with `recoveries[]` | `UNEXPECTED_DIALOG` (dismiss), `SLOW_RESPONSE` (keep waiting, bounded), `SESSION_EXPIRED` (dismiss, re-sign-on, restart once) | Bounded attempts. Every recovery is recorded in the result. |
| Hard failure | `failed` + `failure` | `HOST_ERROR`, `TARGET_NOT_FOUND`, `CHECKPOINT_NOT_MET`, `EXTRACTION_FAILED`, `POLICY_DENIED`, `RECOVERY_EXHAUSTED`, `HUMAN_TIMEOUT`, `ACTION_FAILED` | Stop. Report the step id and intent, what was expected, what was observed, a redacted screenshot, and a redacted tree snapshot. |
| Escalate | continues or `failed` | `SUPERVISOR_OVERRIDE_REQUIRED`, `SIGN_ON_REJECTED`, tenant `DISCLOSURE_CONFIRMATION` | Human handoff on the live session, then re-verify the checkpoint (section 5). |

Sensitive outputs are returned to the caller but fingerprinted in the persisted `result.json`.

**Evidence.** `evidence/replays/summary.md` shows 19 scenarios, all passing:
- the happy path and a second tenant;
- not-found, restricted, and invalid input;
- a popup, a slow host, and session expiry, all recovered;
- a host error, which fails hard;
- an irreversible post with a caller grant, with operator approval, and with no approval, which times out;
- a validation error and a permission denial;
- the supervisor handoff;
- the parameterized share-balance capability reading Share Draft Checking, and returning
  `SHARE_NOT_FOUND` for a share the member doesn't have;
- a product this tenant doesn't offer, which returns `INVALID_OPTION` with the valid choices. It is found by
  the replay's select action, not by a pre-listed rule, so a caller or AI agent can correct the request;
- a read-only capability that lists the account types this tenant offers and the member's funding shares.
  The assistant calls it before opening an account, so it only offers valid choices.

**Drift (secondary).** Locator fallbacks are logged per step. A tenant can override label and name
aliases without re-recording. Structural drift that breaks every strategy fails with `TARGET_NOT_FOUND`
and a full evidence bundle.

## 4. Heterogeneity & multi-tenant

**Surface seam.** Everything above `cua/surface/` sees only `Observation` (windows and nodes with role,
name, value, label, group, and bounds) and a small verb set: click, set text, select, key, and close. The artifact stores semantic targets, not a
technology's selectors.

| Surface | Perceive | Act | What changes in the artifact |
|---|---|---|---|
| Java Swing (built) | In-process accessibility (JAB-equivalent) | Accessibility actions | Nothing |
| Other Windows desktop apps (.NET, PowerBuilder, Delphi) | UI Automation tree | UIA invoke and value patterns, with SendInput fallback | Nothing. Roles are mapped to the same vocabulary. |
| Legacy web (framesets, nested tables) | Browser accessibility tree across frames | CDP or Playwright | `scope.window` becomes a frame path |
| 3270/5250 green screen | Screen buffer fields via an emulator API | Keystrokes | Label strategy maps to a protected field |
| Citrix or pixels only | Screenshot, OCR, and a vision model fill Node with role guesses and bounds | Coordinates | `label` and `coords` strategies carry the load |

Label inference and coordinate locators are already geometric, so they work unchanged on OCR output.

**Multi-tenant reuse.** Capabilities are scoped to a vendor product and a version range, not to a tenant.
The app profile (`configs/apps/corelink.yaml`) holds what is shared by everyone running the product:
- the sign-on routine;
- PII labels;
- the outcome taxonomy;
- a `tenants:` block with small overrides: label and name aliases, extra outcome rules, and the product
  version.

The second tenant, Lakeshore, runs CoreLink 7.6.2. Its "Member #:" label is "Account No.:", its "Inquire"
button is "Search", and it adds a disclosure dialog. The Heritage-recorded capability replays there
unchanged, through the overrides (scenario 09). The resolution order is: capability, then tenant override,
then product. That makes an override a small reviewable diff, not a fork.

**Drift detection at scale.** Four signals, in order of cost:
1. Locator fallback telemetry per capability per tenant.
2. The product version string, read at sign-on, checked against the capability's `versions` range.
3. Scheduled canary replays against a test member per tenant.
4. When a canary fails, a bounded re-discovery on that tenant diffs its artifact against the base. The
   compiler then proposes a tenant override, or a new capability version, for human review.

## 5. Escalation & handoff

**Detecting "stuck."**
- **Discovery:** the model calls `ask_human`, or policy returns require-approval, or the step or time
  budget runs out.
- **Replay:** an `escalate` rule matches, an irreversible step has no grant, or recovery is exhausted.
  Failures are reported and do not wait; see Cuts.

**Control-transfer model.** One holder at a time, enforced inside the app by the bridge:

```
agent --request--> paused --operator claims--> human --operator resolves--> agent
                     \------------- timeout / abort ------------------------> run ends
```

- **`agent`:** the bridge drops real keyboard and mouse input and shows an on-screen banner, so the operator
  cannot fight the automation. The automation's own input is marked as synthetic and passes through.
- **`paused`:** the automation stops touching the app and writes an intervention request. It carries the
  capability or goal, the step id and intent, the reason and rule code, a redacted screenshot, and the
  session address.
- **`human`:** the operator works in the same CoreLink window, with the same process, session, and
  signed-on operator. The bridge records their clicks, typing, and keys, with targets described
  semantically. Password fields are recorded as `[SECRET]`.
- **Resolve:** the operator chooses resume, approve, deny, or abort. The recorded actions are attached to the
  intervention and the run evidence, control returns to `agent`, and the engine re-verifies the current
  step's checkpoint. It never assumes the human left the app in the expected state.

**Operator surface.** The console (`cua console`) is intentionally minimal. It shows the queue, context,
screenshot, a live session view, and the claim and resolve buttons. The CLI has the same verbs. The queue is
a directory of JSON files, so a pager or ticketing hook is a small adapter.

**Evidence.** Scenario 15 shows a supervisor override. The handoff record lists five recorded human
actions and a resume. For unattended evidence, `scripts/sim_operator.py` stands in for the person. It
injects real input events so the recorder sees them. Its interventions are labeled `sim-supervisor` so they
are never mistaken for a real human.

## 6. Safety

**Guardrail model.** Enforced before every action, in discovery and in replay alike:
- an **allowlist** of action verbs, window titles, and keys;
- **risk rules** that classify each target as safe, risky, irreversible, or blocked.

**Irreversible means approval required.** I chose that over a hard block, because the system must do real
work, and over flag-only, because a posted transaction cannot be recalled. Approval comes from one of three
places:
- a human operator;
- a per-invocation grant from the caller, for an approved capability;
- in the member assistant, the member pressing Confirm in the UI. The model cannot confirm for them.

One approval covers one transaction per run. Draft capabilities can't run unattended.

**Identity binding.** The gateway removes caller-bound parameters, such as the signed-in member's own
number, from the tool schema. Bound values override anything the model passes. A prompt-injected "check
member 23456" has nothing to act on.

**Data handling.** Redaction happens before anything leaves memory: logs, evidence, intervention records,
and prompts to the discovery model. Three layers work together:
- patterns for SSNs, dates, card and account numbers, and amounts;
- label rules from the app profile, such as the value next to "SSN:";
- run-scoped literals for inputs and outputs marked sensitive, plus PII values learned from labeled fields
  on each screen, so a name seen next to "Name:" is masked wherever it shows up later.

That fourth source exists because a unit test that scans all evidence for the mock app's PII caught a leak
during development. A member's name reappeared in a "what changed" note to the model, which the
pattern and label rules had missed. The scan now runs as part of the test suite.

Screenshots are redacted by blacking out the accessibility bounds of sensitive nodes. The model never sees
raw PII or credentials. Secrets are referenced by name and resolved from env/.env, standing in for a vault.
Sensitive outputs are persisted only as fingerprints. A unit test fails if an artifact contains a sample
member number, an SSN prefix, a balance, or a credential.

**Limits.**
- Label and pattern redaction can miss PII in free text, such as a note field. Production needs a
  classifier and allowlisted fields.
- The member assistant sends the member's own balance to the model to phrase the answer.
- The live console view is unredacted, which assumes an authorized operator.
- The policy trusts window titles, and a hostile app could spoof them.
- The bridge listens on localhost without authentication.
- Approval grants are not yet cryptographically bound to the reviewed artifact hash.

## 7. Cuts

**Mocked or stubbed, on purpose:**
- **Target app.** CoreLink is a proxy. No real bank system was used.
- **Operator console.** It is minimal, with no browser-based remote input. The operator uses the real
  window.
- **Simulated operator.** It is used for unattended evidence runs only.
- **Member sign-in.** The portal login is a mock.
- **Secret store.** It reads env/.env instead of a vault.
- **Surfaces.** Only one is implemented. UIA, web, and terminal are designed, not built.

**Not built:**
- queues, workers, and multi-session scaling (per the brief);
- tenant-level scheduling;
- artifact storage beyond files plus git;
- authentication on the bridge, console, and gateway;
- LLM-assisted fallback during replay;
- multi-run stability scoring (the scenario matrix is a start).

**Next, in order:**
1. **Bounded assisted fallback.** On `TARGET_NOT_FOUND`, allow one policy-checked LLM re-grounding of that
   single step, then record the proposed locator as a draft override.
2. **Canary replays and drift diffing,** from section 4.
3. **A UI Automation surface,** to prove the seam on a second desktop technology.
4. **Artifact signing.** Approval binds to the hash of the reviewed artifact.
5. **Hardening.** Authentication and mTLS on the bridge, and a PII classifier on free text.
