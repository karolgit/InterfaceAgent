# Evidence

All text and images here are redacted. Member numbers, names, SSNs, dates of birth, addresses, balances,
and credentials are masked. Sensitive outputs appear only as `sha256:` fingerprints.

## discovery/

These are LLM discovery runs, produced by `scripts\demo.ps1` or `python -m cua discover --task ... --out evidence\discovery`.
Each run folder contains the following files.

| File | Contents |
|---|---|
| `log.jsonl` | A structured log of every observation, decision (the model's stated `why`), policy verdict, action result, and reasoning summary |
| `turn-NN.png` | The redacted screenshot the model saw at each turn |
| `discovery_summary.json` | The trace, the success flag, the proof text, and token usage |
| `*.v1.0.0.json` | The capability compiled from this run |

## artifacts/

This folder holds copies of the saved capability artifacts, in schema `cua.capability/v1`.

## replays/

This folder holds a deterministic replay matrix, produced by `python scripts\scenarios.py --out evidence\replays`.
[summary.md](replays/summary.md) lists every scenario, its injected fault, its result, and its code.
Each scenario folder contains the following files.

| File | Contents |
|---|---|
| `log.jsonl` | Step-by-step replay log: locator strategy used, policy verdict, checkpoints, matched outcome rules, recoveries |
| `result.json` | The structured result returned to the caller |
| `*.png` | A redacted screenshot on success, on a business outcome, on a failure, or at a handoff |
| `tree-failure-*.json` | A redacted accessibility snapshot captured on failure |
| `handoff-*.json` | The intervention record: reason, operator, decision, and the recorded human actions |

These are the scenarios worth opening first:

- **Exceptional states:**
  - `02` covers member not found.
  - `07` covers session expiry, where replay signs on again and restarts.
  - `08` covers a host error that fails hard, with a screenshot and a tree snapshot.
- **Human in the loop:**
  - `13` covers an irreversible step with no approval, which times out waiting for a human.
  - `15` covers a supervisor override done by an operator on the live session. Its actions are recorded
    in `handoff-*.json`.

Scenarios 14 and 15 use `scripts/sim_operator.py` in place of a person at the keyboard. Every intervention
it handles is labeled `operator: sim-supervisor`.
