# Demo video script (target 6 to 7 minutes)

This is a shot-by-shot plan. Record each scene as its own clip, then assemble the clips in Canva.
Narration is in quotes. Say it in your own words, since a natural delivery works better than reading.

## Before recording

1. **Screen.** Use 1920x1080 at 100% scaling. Close chat, email, and AI assistant panels.
   Hide personal browser bookmarks.
2. **Terminal.** Use Windows Terminal or PowerShell at font size 16 or larger, with a dark theme. Open it
   in the repo folder and run `.venv\Scripts\activate` once, so commands can start with `python`.
3. **API key.** Put it in `.env`. Scene 3 needs it.
4. **Slow motion for replays.** Real replays finish in about 1.5 seconds. For the recording only, run:
   ```powershell
   $env:CUA_DEMO_DELAY_MS = "700"     # pause 0.7 s after each action so viewers can follow
   ```
5. **Window layout.** Put the terminal on the left half. The CoreLink window opens in the centre, so drag
   it to the right half when it appears. Add `--keep-open` when you want the app to stay up after a run.
6. **Clean state.** Run `Remove-Item runs\interventions\* -ErrorAction SilentlyContinue` so the operator
   console starts empty.
7. **Canva assets.** Use `docs/architecture.png`, `docs/screenshots/*.png`, and
   `evidence/replays/summary.md` (screenshot the table in VS Code preview).

## Scenes

| # | Time | Scene | On screen | Narration |
|---|---|---|---|---|
| 0 | 0:00 to 0:10 | Title card (Canva) | "InterfaceAgent: computer-use automation for legacy banking apps", your name, repo URL | none, or music |
| 1 | 0:10 to 0:40 | The problem and the idea | `docs/architecture.png`, full screen; zoom slowly from left to right | "Banks run legacy apps with no API. This system uses an LLM to learn a task once, turns that run into a typed, versioned capability, and replays it deterministically with no model in the loop. When it can't safely continue, it hands the live session to a human." |
| 2 | 0:40 to 1:10 | The legacy target | Run `python -m cua app`. Sign on as teller01 / demo123. Press F2, look up 12345, then press F5 to show the form. | "The proxy target is CoreLink, a Java Swing teller app I built to be hostile to automation. There's no DOM and no test IDs, it uses internal windows and modal dialogs, and on this form the labels aren't even linked to their fields. Faults can be injected at runtime." Close the app afterwards. |
| 3 | 1:10 to 2:10 | **Real LLM discovery** (the most important scene) | Run `python -m cua discover --task tasks\get_savings_balance.yaml --keep-open`. Show the app moving. Then open the newest `runs\discover-*` folder: two `turn-NN.png` files and `log.jsonl`, scrolled to a `decision` line with its `why`. | "Here the agent gets only a goal and typed inputs and outputs. Each turn it sees a redacted accessibility outline and screenshot, picks one action on an element reference, and every action passes through policy first. It refers to the member number by parameter name and never sees it. The screenshots it saw are redacted, and every decision is logged with its reason." |
| 4 | 2:10 to 2:55 | The artifact | Open `capabilities\corelink.member.get_share_savings_balance.v1.0.0.json` in VS Code. Show inputs and outputs, `business_outcomes`, one step's `strategies`, `value: param`, `expect`, and `risk`. | "The run is compiled into a capability, decoupled from the model transcript. The top is the contract a calling agent reads: typed inputs, outputs, and business outcomes. Each step has ranked locators, from accessible name down to coordinates, each checked for uniqueness when recorded. Values come from parameters or named secrets, never literals, and every step has a checkpoint." |
| 5 | 2:55 to 3:15 | Deterministic replay | Run `python -m cua approve corelink.member.get_share_savings_balance --reviewer <you>`, then `python -m cua replay corelink.member.get_share_savings_balance -p member_number=12345 --keep-open` | "A reviewer approves it, and now it replays without the LLM. It returns a typed result: the balance." |
| 6 | 3:15 to 4:15 | Error taxonomy | Three commands in turn. First `... -p member_number=99999`, which gives a business outcome. Then `... -p member_number=12345 --fault session_expired=true`, which shows `recoveries: SESSION_EXPIRED` and success. Then `... --fault app_error=true`, which fails with step, expected, observed, and evidence paths. Open the failure screenshot. | "What matters is how replay handles runtime conditions. 'No such member' is a business outcome the caller needs, not a crash. A session timeout is recoverable: it signs on again and restarts, and the recovery is recorded. A host error is a hard failure, reported with the step, what was expected, what was observed, and a redacted screenshot and tree snapshot." |
| 7 | 4:15 to 5:15 | **Human handoff on the live session** | Terminal 1: `python -m cua console`, then open http://127.0.0.1:8765. Terminal 2: `python -m cua replay corelink.account.open_share_sub_account -p member_number=23456 -p "account_type=Money Market" -p initial_deposit=12000.00 -p fund_from=S10 --approve-irreversible --allow-draft --keep-open`. While the red banner shows, try clicking the app and point out that nothing happens. When the override dialog appears, the request shows in the console. Click **Take control**, type sup01 / demo123 in the CoreLink dialog, press OK, then click **resume**. Show the result, then open `runs\replay-*\handoff-*.json`. | "Deposits over ten thousand dollars need a supervisor. The automation pauses and raises an intervention with context and a screenshot. While the automation holds control, my input is blocked, as the red banner shows. When I take control, the same live session is mine: same window, same signed-on teller. My actions are recorded. When I resume, replay re-checks the checkpoint and finishes the posting." |
| 8 | 5:15 to 5:35 | Safety | Show `configs\policies\corelink.yaml`, then `docs\screenshots\10-redacted-evidence.png` | "Every action, in discovery and replay, goes through an allowlist and risk classes. Posting is irreversible, so it needs an approval grant or a human. Evidence and prompts are redacted, and screenshots are blacked out using accessibility bounds." |
| 9 | 5:35 to 5:55 | Second tenant | Run `python -m cua replay corelink.member.get_share_savings_balance -p member_number=23456 --tenant lakeshore --keep-open` | "Another credit union runs the same product, a newer version with renamed controls. The same capability replays through small tenant overrides instead of being re-recorded." |
| 10 | 5:55 to 6:35 | Member assistant with voice | Run `python -m portal.app --allow-draft`, then open http://127.0.0.1:8800. Sign in as Jane. Press **Mic** and say "What's my savings balance?" Then say "Open a Christmas club with 50 dollars from savings" and press **Confirm**. | "On top of this, an agent calls capabilities as tools. The member's identity comes from the login, never from the model, and irreversible actions wait for the member to confirm." |
| 11 | 6:35 to 6:50 | Proof | `python -m pytest -q tests`, then the `evidence\replays\summary.md` preview | "Unit tests cover locators, policy, and redaction. Seventeen replay scenarios cover every outcome class, with evidence in the repo." |
| 12 | 6:50 to 7:05 | Close (Canva) | Card: "Cut: queues and scaling, real co-browsing, other surfaces. Next: bounded LLM fallback for a single step, canary replays, a UI Automation surface." Repo URL. | "Thanks for watching. The design trade-offs are in REPORT.md." |

## Canva editing tips

- Speed up the waiting in scene 3 (discovery) to 2x. Keep the first and last turns at normal speed.
- Zoom in on the terminal when a result JSON appears, and on the red and green control banner in scene 7.
- Add burned-in captions (Canva's auto-captions). Reviewers often watch muted.
- Show a small lower-third label per scene, such as "Deterministic replay: no LLM."
- Export 1080p MP4. Upload to YouTube as unlisted, or to Google Drive with link sharing, and put the link in
  `README.md` under the title.

## If something goes wrong while recording

| Symptom | Fix |
|---|---|
| "no bridge on port 8740" or the app won't start | Close any leftover CoreLink windows. Kill `java.exe` from Task Manager if needed. |
| Port already in use | Run `Get-Process java, python \| Stop-Process` (check that nothing else of yours runs) |
| Discovery fails or loops | Save the `runs\discover-*` folder and share `log.jsonl`. The prompt or task may need tuning. |
| Handoff never appears in the console | Make sure the console runs from the same repo folder. It reads `runs\interventions\`. |
