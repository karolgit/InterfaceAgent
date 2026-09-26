"""Replay scenario matrix: happy path plus each runtime condition, one fresh app per scenario.

    python scripts/scenarios.py [--out evidence/replays] [--only name,name]

Writes one evidence folder per scenario and a summary.json / summary.md table.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cua.runtime import app_session, load_capability, run_replay, write_faults  # noqa: E402

BAL = "corelink.member.get_share_savings_balance"
OPEN = "corelink.account.open_share_sub_account"
SHARE = "corelink.member.get_share_balance"
OPEN_OK = {"member_number": "12345", "account_type": "Share Certificate", "initial_deposit": "1000.00",
           "fund_from": "S01"}

# name: (capability, params, faults, tenant, extra kwargs, expected status/code)
SCENARIOS = {
    "01-balance-happy-path": (BAL, {"member_number": "12345"}, {}, "heritage", {}, "success"),
    "02-balance-member-not-found": (BAL, {"member_number": "99999"}, {}, "heritage", {}, "MEMBER_NOT_FOUND"),
    "03-balance-invalid-input": (BAL, {"member_number": "12AB5"}, {}, "heritage", {}, "INVALID_INPUT"),
    "04-balance-member-restricted": (BAL, {"member_number": "34567"}, {}, "heritage", {}, "MEMBER_RESTRICTED"),
    "05-balance-unexpected-popup": (BAL, {"member_number": "12345"}, {"popup": "true"}, "heritage", {}, "success"),
    "06-balance-slow-host": (BAL, {"member_number": "12345"}, {"slow_ms": "4500"}, "heritage", {}, "success"),
    "07-balance-session-expired": (BAL, {"member_number": "12345"}, {"session_expired": "true"}, "heritage", {},
                                   "success"),
    "08-balance-host-error": (BAL, {"member_number": "12345"}, {"app_error": "true"}, "heritage", {}, "HOST_ERROR"),
    "09-balance-second-tenant": (BAL, {"member_number": "23456"}, {}, "lakeshore", {}, "success"),
    "10-open-account-approved": (OPEN, OPEN_OK, {}, "heritage", {"approve_irreversible": True}, "success"),
    "11-open-account-below-minimum": (OPEN, {**OPEN_OK, "initial_deposit": "100.00"}, {}, "heritage",
                                      {"approve_irreversible": True}, "VALIDATION_ERROR"),
    "12-open-account-permission-denied": (OPEN, OPEN_OK, {}, "heritage",
                                          {"approve_irreversible": True, "operator": "viewer01"}, "PERMISSION_DENIED"),
    "13-open-account-no-approval-grant": (OPEN, OPEN_OK, {}, "heritage", {"handoff_timeout_s": 6}, "HUMAN_TIMEOUT"),
    "14-open-account-operator-approves": (OPEN, {**OPEN_OK, "account_type": "Christmas Club", "initial_deposit": "50.00"},
                                          {}, "heritage", {"sim": ["--approve"]}, "success"),
    "15-open-account-supervisor-handoff": (OPEN, {"member_number": "23456", "account_type": "Money Market",
                                                  "initial_deposit": "12000.00", "fund_from": "S10"}, {}, "heritage",
                                           {"approve_irreversible": True, "sim": ["--supervisor"]}, "success"),
    "16-share-balance-checking": (SHARE, {"member_number": "12345", "share_type": "Share Draft Checking"}, {},
                                  "heritage", {}, "success"),
    "17-share-balance-share-not-found": (SHARE, {"member_number": "12345", "share_type": "Money Market"}, {},
                                         "heritage", {}, "SHARE_NOT_FOUND"),
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "runs" / "scenarios"))
    ap.add_argument("--only")
    ap.add_argument("--allow-draft", action="store_true", default=True)
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    only = set(args.only.split(",")) if args.only else None
    rows = []
    for name, (cap_id, params, faults, tenant, kw, expected) in SCENARIOS.items():
        if only and not any(o in name for o in only):
            continue
        kw = dict(kw)
        operator = kw.pop("operator", None)
        if operator:
            os.environ["CORELINK_OPERATOR_ID"] = operator
        else:
            os.environ.pop("CORELINK_OPERATOR_ID", None)
        try:
            cap = load_capability(cap_id)
        except FileNotFoundError:
            print(f"skip {name}: no capability {cap_id}")
            continue
        write_faults(faults)
        sim_args = kw.pop("sim", None)
        t0 = time.monotonic()
        with app_session(tenant) as s:
            sim = None
            if sim_args:  # SIMULATED operator stands in for a human at the keyboard
                sim = subprocess.Popen([sys.executable, str(ROOT / "scripts" / "sim_operator.py"), *sim_args,
                                        "--once", "--timeout", "90"])
            try:
                res = run_replay(cap, params, s, tenant, allow_draft=args.allow_draft, out_dir=out,
                                 run_prefix=name, **kw)
            finally:
                if sim:
                    sim.kill()
        write_faults({})
        code = res.status if res.status == "success" else (
            (res.outcome or {}).get("code") or (res.failure.code if res.failure else res.status))
        ok = code == expected
        rows.append({"scenario": name, "tenant": tenant, "faults": faults, "status": res.status, "code": code,
                     "expected": expected, "pass": ok, "recoveries": [r["code"] for r in res.recoveries],
                     "fallbacks": len(res.locator_fallbacks), "handoffs": [h["decision"] for h in res.handoffs],
                     "seconds": round(time.monotonic() - t0, 1), "run_dir": Path(res.evidence_dir).name})
        print(f"{'PASS' if ok else 'FAIL'}  {name:38} status={res.status:17} code={code}")
    os.environ.pop("CORELINK_OPERATOR_ID", None)
    (out / "summary.json").write_text(json.dumps(rows, indent=2))
    md = ["| Scenario | Tenant | Injected | Result | Code | Recoveries | Pass |", "|---|---|---|---|---|---|---|"]
    for r in rows:
        inj = ", ".join(f"{k}={v}" for k, v in r["faults"].items()) or "-"
        md.append(f"| {r['scenario']} | {r['tenant']} | {inj} | {r['status']} | {r['code']} | "
                  f"{', '.join(r['recoveries']) or '-'} | {'yes' if r['pass'] else 'NO'} |")
    (out / "summary.md").write_text("\n".join(md) + "\n")
    return 0 if all(r["pass"] for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
