"""Build the submission report: docs/InterfaceAgent-Report.pdf (and the HTML it is printed from).

    python scripts/build_report.py

Sources: REPORT.md (design write-up), evidence/replays/summary.md, capabilities/*.json,
docs/architecture.svg, docs/screenshots/. Needs Microsoft Edge (or Chrome) for PDF printing.
"""
from __future__ import annotations

import json
import re
import subprocess
from datetime import date
from pathlib import Path

import markdown

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "docs" / "report"
BROWSERS = [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Google\Chrome\Application\chrome.exe"]
REPO = "https://github.com/karolgit/InterfaceAgent"

COVERAGE = """
| Brief requirement | How it is met | Where in the repo |
|---|---|---|
| **3.1** Goal + target in, observe, decide, act loop, stop conditions | A task YAML declares the goal, typed inputs, and outputs. The loop takes one action per turn. It stops on max steps, timeout, `finish`, or the human aborting. | `cua/agent/discover.py`, `tasks/` |
| **3.1** Real UI, works without a clean DOM | Perception is the accessibility tree plus a screenshot of an unmodified Swing desktop app. Actions go through accessibility actions. | `bridge/`, `cua/surface/` |
| **3.2** Typed, versioned, reviewable artifact | `cua.capability/v1` splits a contract (inputs, outputs, outcomes, risk) from a flow (steps, locators, checkpoints). It carries semver, draft or approved status, and provenance. JSON Schema is exported. | `cua/artifact.py`, `schemas/`, `capabilities/` |
| **3.2** How targets are identified, and why that is robust | Five ranked strategies: a11y name, label, group, index path, then coordinates. Each records whether it was unique at record time. Replay reports fallbacks. | `cua/locate.py` |
| **3.3** Replay with no LLM, stable targeting, checkpoints, outputs | Fixed start state, locator resolution with waits, checkpoint waits instead of sleeps, typed extraction, and success verification. | `cua/replay.py` |
| **3.3** Business outcome vs recoverable vs hard failure | Outcome rules come from the product profile. Results are `business_outcome` (with a code), `success` (with `recoveries[]`), or `failed` (step, expected, observed, evidence). | `configs/apps/corelink.yaml`, `cua/replay.py` |
| **3.4** Configurable allowlist, risky actions handled conservatively | Actions, windows, and keys are allowlisted. Every target gets a risk class. Irreversible actions need an approval grant or a human. | `configs/policies/corelink.yaml`, `cua/policy.py` |
| **3.4** No secrets or raw PII persisted | Secrets are referenced by name. Three-layer text redaction, screenshot blackout from accessibility bounds, and fingerprinted sensitive outputs. A unit test guards the artifacts. | `cua/redact.py`, `cua/secrets.py`, `tests/` |
| **3.5** Structured log plus a richer failure signal | JSONL log of every decision (with the model's `why`), policy verdict, and checkpoint. Redacted screenshots and accessibility tree dumps on failure. | `cua/evidence.py`, `evidence/` |
| **3.6** Detect and route with context | The intervention request carries the capability or goal, the step, the reason code, a redacted screenshot, and the session. | `cua/handoff.py` |
| **3.6** Human takes the same live session, then hands back | Control lock (agent, paused, human) inside the app. Human input is blocked while the agent holds control and recorded while the human does. Resume re-verifies the checkpoint. | `bridge/`, `cua/handoff.py`, `cua/console.py` |
| **3.7** Surface abstraction | `Observation` and `Node` plus a small verb set. Surface-specific code lives only in drivers. | `cua/surface/base.py`, REPORT §4 |
| **3.7** Multi-tenant reuse and drift | A product-level app profile with tenant overrides. The Heritage-recorded capability replays on Lakeshore (v7.6.2, renamed controls, extra dialog). | `configs/apps/corelink.yaml`, scenario 09 |
| **Stretch** Agent-facing capability interface | A gateway exposes approved capabilities as typed tools. A member assistant (chat and voice) invokes them, with identity binding and member confirmation. | `cua/gateway.py`, `portal/` |
| **Stretch** Confidence and approval | Unattended replay requires `approved`. `cua approve` records the reviewer. | `cua/replay.py`, `cua/__main__.py` |
| **Stretch** Canonicalization: one capability, many inputs | The discovered row filter becomes a typed `share_type` input, so one recording reads any share. A missing share returns the business outcome `SHARE_NOT_FOUND`. | `scripts/derive_share_balance.py`, scenarios 16 and 17 |
| **Stretch** Cross-tenant reuse with per-variant overrides | Tenant label and name aliases, plus extra outcome rules. | `configs/apps/corelink.yaml` |
"""

DEMO = """
```
.\\scripts\\build.ps1                                        # setup: venv, portable JDK, Java builds
python -m cua discover --task tasks\\get_savings_balance.yaml   # 1. LLM discovery, then artifact
python -m cua catalog                                        # 2. contract view
python -m cua approve corelink.member.get_share_savings_balance --reviewer <name>
python -m cua replay corelink.member.get_share_savings_balance -p member_number=12345      # 3. replay
python -m cua replay corelink.member.get_share_savings_balance -p member_number=99999      # 4. not found
python -m cua replay ... --fault session_expired=true | --fault app_error=true             #    recover / fail
python -m cua console                                        # 5. operator console (handoff)
python scripts\\scenarios.py --out evidence\\replays           # 6. full scenario matrix
python -m portal.app --allow-draft                           # 7. member assistant, chat + voice
```
"""

RECORDING = """
Recording does not capture mouse movements or video. It records **which control** was used, **where each value
came from**, and **what changed on screen**. These examples come from the real Claude discovery run for "get
savings balance".

**Each turn of the discovery loop**

1. **Look.** The surface driver reads the app's accessibility tree (roles, names, labels, states, bounds) and turns
   it into an outline with short references. The model gets that outline plus a redacted screenshot:

```
[w0.0.1.1.1.0] menu item "Member Inquiry"   (in closed menu; clickable)
[w0.0.1.0.1.0.1.0.0.1] text  label="Member #:"
[w0.0.1.0.1.0.1.0.0.2] push button "Inquire"
```

2. **Decide.** The model picks exactly one action and states why, for example
   `click(ref="w0.0.1.1.1.0", why="Open the Member Inquiry screen")`. Inputs are referenced by name
   (`type_text(ref=..., param="member_number")`). The model never sees or types the value.
3. **Check.** Policy verifies the action against the allowlist and assigns its risk class. Irreversible actions
   pause for a human.
4. **Act.** The driver performs the action through accessibility, not the physical mouse:

| Action | How it is performed |
|---|---|
| Click a button or menu item | The control's accessibility action, the same effect as a click |
| Type into a field | Sets the field's text through the accessible text interface. That focuses the field first, so "focus" is never a separate step. |
| Press a key (F2, Enter, Esc) | Key events sent to the app window |
| Choose from a drop-down | Selects the matching option. If the option doesn't exist, the result is `INVALID_OPTION`. |

5. **Record.** For each action the trace keeps the target's role, name, label, panel, index path, and bounds. It
   also keeps the value source (literal, input parameter, or named secret), the screen before and after, the
   policy verdict, and the model's reason.

**Compiling the trace into the capability**

When the model calls `finish`, the compiler converts the trace into the artifact, independent of the model
transcript:

- **Targets become ranked locators**, each checked for uniqueness on the recorded screen. The order is
  accessible name, then nearby label, then named panel, then index path, then relative coordinates as a last resort.
- **Typed values become parameters or secret references**, never literal PII.
- **Screen changes become checkpoints**: a new work window, a new dialog, a new status line (numbers
  generalized, as in `INQUIRY COMPLETE - \\d+ SHARE\\(S\\)`), or a field holding a value.
- **Policy verdicts become per-step risk classes.** The model's quoted proof becomes the final success check.

```
s2  set_text   value = input "member_number"
    locate by: 1) name "Member #:"  2) label "Member #:"  3) index path  4) coordinates
    checkpoint: field holds a value
s3  click "Inquire"
    checkpoint: status line matches "INQUIRY COMPLETE - \\d+ SHARE\\(S\\)"
```

In replay there is no model. Each step re-finds its control with those strategies, acts, and waits for its checkpoint,
so a moved window or a slightly renamed label does not break it. Human actions during a handoff are recorded
separately by the driver's input recorder: real clicks and keystrokes, with password fields stored as `[SECRET]`.
"""

SURFACES = """
Only one component is specific to Java Swing: the **surface driver** (the bridge). Everything above it is
technology-neutral and does not change:
- the discovery agent;
- the artifact schema;
- the replay engine;
- policy and redaction;
- the capability gateway;
- the operator console.

Supporting another kind of application means writing another driver behind the same interface. It must provide
observe (nodes with role, name, value, label, and bounds), the basic actions (click, set text, select, key, close),
screenshots, and the control lock with input recording.

| Application type | Driver | How it perceives and acts |
|---|---|---|
| Java desktop (CoreLink) | Accessibility bridge (**built**) | Java Accessibility API in-process; accessibility actions |
| Modern or legacy web app | Browser driver (Playwright or CDP) | The browser's accessibility tree and page structure, including frames and framesets; browser input |
| Windows desktop (.NET, Win32, Delphi, PowerBuilder) | Windows UI Automation driver | The Windows accessibility tree; invoke and value patterns, with synthesized input as a fallback |
| Custom-drawn UIs, Citrix, or remote desktops | Vision driver | Screenshot, OCR, and a vision model produce nodes with role guesses and bounds; coordinate input |
| Mainframe green screens (3270/5250) | Terminal-emulator driver | Screen buffer fields; keystrokes |

**What changes per application.**
- **App profile.** A new `configs/apps/<product>.yaml` declares the driver, the sign-on routine, the product's
  error messages mapped to outcome codes, the PII labels, and tenant overrides.
- **Scope.** `scope.window` may name a browser frame or page instead of a desktop window.

**What does not change.**
- **The artifact format and locator strategies.** Label proximity and relative coordinates are geometric, so a
  vision driver fills them from OCR bounds exactly as the Swing driver fills them from accessibility bounds.
- **Replay, the error taxonomy, human handoff, and redaction.** They work unchanged on top of any driver.
"""

LAYOUT = """
**Capability artifacts** are JSON files in `capabilities/`, named `<id>.v<version>.json`. Copies sit in
`evidence/artifacts/`. Each file is one capability: its contract (inputs, outputs, business outcomes, risk)
and its flow (steps, ranked locators, checkpoints). There are four today:

| Capability | Origin | Risk |
|---|---|---|
| `corelink.member.get_share_savings_balance` | Discovered by Claude Opus 5 | Read-only |
| `corelink.account.open_share_sub_account` | Discovered by Claude Opus 5 (post approved by a human) | Irreversible |
| `corelink.member.get_share_balance` | Derived: row filter turned into a `share_type` input | Read-only |
| `corelink.account.get_open_options` | Derived: reads the offered account types and funding shares | Read-only |

**JSON Schemas** in `schemas/` are the formal rulebooks for the two file formats, generated from the Python models
with `python -m cua schema`:
- `capability.schema.json` defines what a valid capability artifact must contain. Reviewers, CI, or tools in other
  languages can validate artifacts against it.
- `run_result.schema.json` defines what a replay returns to its caller:
  - `status`: success, business_outcome, failed, or rejected;
  - `outputs`;
  - `outcome`, or `failure` with step, expected, observed, and evidence;
  - `recoveries`, `handoffs`, and `locator_fallbacks`.

```
computeruseagents/
├─ README.md, REPORT.md          how to run it; the design write-up
├─ .env.example                  settings template (copy to .env for API keys; .env is never committed)
├─ mockcore/                     CoreLink: the legacy-style Java Swing target app, with faults and two tenants
├─ bridge/                       Java agent loaded into CoreLink: accessibility tree, actions,
│                                screenshots, control lock, human-input recorder
├─ cua/                          the core system (Python)
│  ├─ surface/                   driver interface (base.py) and the CoreLink driver (swing.py)
│  ├─ agent/                     discovery loop, model tool surface, trace-to-artifact compiler
│  ├─ artifact.py                capability and run-result models (schema v1)
│  ├─ replay.py                  deterministic replay and outcome classification
│  ├─ locate.py                  locator building (record time) and resolution (replay time)
│  ├─ policy.py, redact.py       allowlist and risk classes; PII redaction
│  ├─ handoff.py, console.py     control transfer, intervention queue, operator console
│  ├─ gateway.py                 capability catalog and invoke path for agents
│  └─ runtime.py, __main__.py    wiring and the `python -m cua` command line
├─ configs/apps/corelink.yaml    vendor-product profile: sign-on, outcome rules, PII labels, tenant overrides
├─ configs/policies/             safety policy (allowlist, risk rules)
├─ tasks/                        capability requests given to discovery
├─ capabilities/                 saved capability artifacts (JSON)
├─ schemas/                      JSON Schemas for the artifact and the run result
├─ portal/                       Member Assistant web app (chat, voice, staff mode, built-in console)
├─ evidence/                     discovery runs, 19 replay scenarios, artifact copies (all redacted)
├─ docs/                         this report, architecture diagram, screenshots
├─ scripts/                      build, demo, scenario matrix, capability derivation, report builder,
│                                screenshot capture, simulated operator
└─ tests/                        unit tests: locators, policy, redaction, replay rules, evidence PII scan
```

These are generated or local only, and never committed:
- `.venv/`, the Python environment;
- `.tools/`, the portable JDK;
- `runs/`, scratch runs and the intervention queue;
- `.env`, with API keys;
- the compiled Java output.
"""

SHOTS = [
    ("01-corelink-sign-on.png", "Sign-on. Credentials are resolved from the secret store by name."),
    ("02-member-inquiry.png", "Member Inquiry: the flow the first capability automates."),
    ("03-member-not-found.png", "Business outcome: MBR NOT ON FILE becomes MEMBER_NOT_FOUND."),
    ("04-open-sub-account-form.png", "The labels are not linked to their fields. Replay locates them by label geometry."),
    ("05-review-transaction.png", "The review screen, before the irreversible post."),
    ("06-confirm-posting-dialog.png", "An irreversible confirmation. Policy requires approval."),
    ("07-transaction-posted.png", "The output: a confirmation number extracted with a capture pattern."),
    ("08-supervisor-override.png", "Escalation: a supervisor override is handed to a human."),
    ("09-second-tenant-lakeshore.png", "Second tenant (v7.6.2, renamed controls). The same capability replays through overrides."),
    ("10-redacted-evidence.png", "Evidence screenshot, redacted using accessibility bounds."),
    ("11-operator-console.png", "Operator console: the queue, context, redacted snapshot, and live session."),
    ("12-member-assistant.png", "Member assistant answering from a deterministic replay."),
    ("13-member-confirmation.png", "The member must confirm an irreversible action. The model cannot."),
    ("16-open-account-options.png", "Valid account types and funding shares, read live from the core system, offered as buttons."),
    ("14-staff-assistant.png", "Staff mode: the employee names the member; follow-ups reuse it."),
    ("15-portal-landing.png", "Member Assistant landing page with member and staff sign-in."),
]

CSS = """
@page { size: Letter; margin: 16mm 15mm 16mm 15mm; }
* { box-sizing: border-box; }
body { font: 10.2pt/1.45 "Segoe UI", Helvetica, Arial, sans-serif; color: #17202e; margin: 0; }
h1 { font-size: 20pt; margin: 0 0 6pt; color: #1f3a68; }
h2 { font-size: 14.5pt; color: #1f3a68; border-bottom: 1.5px solid #c9d3e3; padding-bottom: 3pt; margin: 18pt 0 8pt; }
h3 { font-size: 11.5pt; margin: 12pt 0 4pt; }
p, li { orphans: 3; widows: 3; }
table { border-collapse: collapse; width: 100%; margin: 6pt 0 10pt; font-size: 8.8pt; page-break-inside: auto; }
tr { page-break-inside: avoid; }
th, td { border: 1px solid #d3d9e3; padding: 4pt 5pt; vertical-align: top; text-align: left; }
th { background: #eef2f8; }
code { font: 8.6pt Consolas, "Courier New", monospace; background: #f3f5f8; padding: 0 2pt; border-radius: 2px; }
pre { background: #f5f7fa; border: 1px solid #dde2ea; border-radius: 4px; padding: 7pt; font-size: 8.2pt; white-space: pre-wrap;
      page-break-inside: avoid; }
pre code { background: none; padding: 0; }
.cover { height: 245mm; display: flex; flex-direction: column; justify-content: center; }
.cover .kicker { color: #5b6474; font-size: 11pt; letter-spacing: .5px; text-transform: uppercase; }
.cover h1 { font-size: 30pt; margin: 8pt 0; }
.cover .sub { font-size: 13pt; color: #3c4658; max-width: 150mm; }
.cover .meta { margin-top: 26pt; font-size: 10.5pt; color: #3c4658; line-height: 1.7; }
.cover img { margin-top: 26pt; width: 100%; border: 1px solid #dde2ea; border-radius: 6px; }
.pb { page-break-before: always; }
.fig { width: 100%; border: 1px solid #dde2ea; border-radius: 4px; }
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10pt; }
.grid figure { margin: 0; page-break-inside: avoid; }
.grid img { width: 100%; border: 1px solid #dde2ea; border-radius: 4px; }
figcaption { font-size: 8.4pt; color: #4b5566; margin-top: 2pt; }
.note { border-left: 3px solid #b39200; background: #fffbe8; padding: 6pt 9pt; font-size: 9.2pt; margin: 8pt 0; }
"""


def md(text: str) -> str:
    return markdown.markdown(text, extensions=["tables", "fenced_code"])


def discovery_status() -> str:
    runs = sorted((ROOT / "evidence" / "discovery").glob("discover-*/discovery_summary.json")) \
        if (ROOT / "evidence" / "discovery").exists() else []
    llm = [json.loads(p.read_text()) for p in runs]
    llm = [r for r in llm if str(r.get("planner", "")).startswith("llm:")]
    if not llm:
        return ('<div class="note"><b>Discovery evidence status:</b> no LLM discovery run is recorded in '
                '<code>evidence/discovery/</code> yet. Run <code>scripts\\demo.ps1</code> with an API key, then '
                'rebuild this report.</div>')
    rows = "".join(f"<tr><td>{r['run_id']}</td><td>{r['planner']}</td><td>{'yes' if r['success'] else 'no'}</td>"
                   f"<td>{r['steps']}</td><td>{r.get('usage', {}).get('input_tokens', '')}/"
                   f"{r.get('usage', {}).get('output_tokens', '')}</td></tr>" for r in llm)
    return ("<table><tr><th>Discovery run</th><th>Planner</th><th>Success</th><th>Actions</th>"
            f"<th>Tokens in/out</th></tr>{rows}</table>")


def artifact_excerpt() -> str:
    p = ROOT / "capabilities" / "corelink.member.get_share_savings_balance.v1.0.0.json"
    if not p.exists():
        return "<p>(artifact not found)</p>"
    d = json.loads(p.read_text())
    contract = {k: d[k] for k in ("id", "version", "status", "risk", "inputs", "outputs")}
    contract["business_outcomes"] = [b["code"] for b in d["business_outcomes"]]
    step = next(s for s in d["steps"] if s["action"] == "set_text")
    return ("<h3>Contract (what a calling agent sees)</h3><pre><code>" + json.dumps(contract, indent=2) +
            "</code></pre><h3>One flow step (what replay executes)</h3><pre><code>" + json.dumps(step, indent=2) +
            "</code></pre>")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    report_md = (ROOT / "REPORT.md").read_text(encoding="utf-8")
    report_md = re.sub(r"^# REPORT\s*", "", report_md)
    summary = ROOT / "evidence" / "replays" / "summary.md"
    scen = md(summary.read_text()) if summary.exists() else "<p>(run scripts/scenarios.py)</p>"
    svg = (ROOT / "docs" / "architecture.svg").read_text(encoding="utf-8")
    svg = svg.replace('width="1300" height="640"', 'width="100%"')
    shots = "".join(f'<figure><img src="../screenshots/{f}"><figcaption>{c}</figcaption></figure>'
                    for f, c in SHOTS if (ROOT / "docs" / "screenshots" / f).exists())
    html = f"""<!doctype html><html><head><meta charset="utf-8"><title>InterfaceAgent Report</title>
<style>{CSS}</style></head><body>
<section class="cover">
  <div class="kicker">interface.ai take-home · Computer-Use Automation System</div>
  <h1>InterfaceAgent</h1>
  <div class="sub">An LLM discovers how to do a task in a legacy banking app once. The run becomes a typed, versioned
  capability that replays deterministically, classifies runtime errors, stays inside safety policy, and hands the
  live session to a human when it must.</div>
  <div class="meta">Karol Stuart · {date.today().strftime('%B %d, %Y')}<br>Repository: {REPO}</div>
  <img src="../user-flow.png">
</section>

<section class="pb"><h2>Summary</h2>
{md('''The target is **CoreLink**, a legacy-style Java Swing core-banking desktop client built as the proxy app. It has no
DOM and no test IDs, its labels aren't linked to their fields, it uses internal frames and modal dialogs, and faults can
be injected at runtime. All data is fake. Four pieces carry the design:

- **A surface driver.** A Java agent reads the unmodified app's accessibility tree, acts through accessibility
  actions, and owns the human-handoff control lock.
- **A discovery agent.** A pluggable LLM planner (Claude or Gemini) acts on element references and never sees raw PII.
- **A capability artifact.** It has a contract part for calling agents and a flow part for replay.
- **A replay engine.** It sorts every run into a business outcome, a recovered condition, or a hard failure.

The thread runs end to end: goal, LLM run, saved capability, deterministic replay with parameters, outputs, and error
handling, human handoff on the live session, and evidence for each. Every core requirement in the brief is covered, as
the matrix below shows. The stretch goals built are the agent-facing capability interface and
cross-tenant reuse with per-tenant overrides. Unattended replay is also gated on draft or approved status.
''')}
{discovery_status()}
<h2>Requirements coverage</h2>{md(COVERAGE)}</section>

<section class="pb"><h2>Architecture diagram</h2>
<p>The cover shows the user flow. This is the component view: what each part does and how the parts connect.</p>{svg}</section>

<section class="pb"><h2>How a capability is recorded</h2>{md(RECORDING)}</section>

<section class="pb"><h2>Supporting applications that are not Java</h2>{md(SURFACES)}</section>

<section class="pb"><h2>Where things live in the repository</h2>{md(LAYOUT)}</section>

<section class="pb">{md(report_md)}</section>

<section class="pb"><h2>Appendix A. Demo commands</h2>{md(DEMO)}
<h2>Appendix B. Replay scenario results</h2>{scen}
<p>Each scenario runs in a fresh app instance. The evidence for each is in <code>evidence/replays/&lt;scenario&gt;/</code>.
Scenarios 14 and 15 use a clearly labeled simulated operator in place of a person at the keyboard.</p></section>

<section class="pb"><h2>Appendix C. Artifact excerpt</h2>{artifact_excerpt()}</section>

<section class="pb"><h2>Appendix D. Screens</h2><div class="grid">{shots}</div></section>
</body></html>"""
    html_path = OUT_DIR / "InterfaceAgent-Report.html"
    html_path.write_text(html, encoding="utf-8")
    pdf = ROOT / "docs" / "InterfaceAgent-Report.pdf"
    browser = next((b for b in BROWSERS if Path(b).exists()), None)
    if not browser:
        print(f"HTML written to {html_path}; no Edge/Chrome found for PDF")
        return
    subprocess.run([browser, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                    f"--print-to-pdf={pdf}", html_path.as_uri()], check=True, capture_output=True, timeout=120)
    print(f"wrote {pdf} ({pdf.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
