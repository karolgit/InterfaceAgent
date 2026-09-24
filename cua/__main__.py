"""CLI.  python -m cua <command> ...

  app        launch CoreLink with the bridge and leave it running (for manual use / --attach)
  discover   run the LLM discovery agent on a task and compile a capability artifact
  replay     replay a capability deterministically with input params
  catalog    list capabilities as an agent sees them (contract only)
  approve    mark a draft capability approved (records the reviewer)
  operator   list / claim / resolve human intervention requests
  console    start the web operator console
  schema     write the artifact JSON Schema to schemas/
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _kv(items: list[str] | None) -> dict[str, str]:
    out = {}
    for it in items or []:
        k, _, v = it.partition("=")
        out[k] = v
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="cua")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("app")
    a.add_argument("--tenant", default="heritage")
    a.add_argument("--port", type=int, default=8740)

    d = sub.add_parser("discover")
    d.add_argument("--task", required=True)
    d.add_argument("--tenant", default="heritage")
    d.add_argument("--planner", default="claude", choices=["claude", "scripted"])
    d.add_argument("--script", help="scripted planner file (offline tests only)")
    d.add_argument("--model")
    d.add_argument("--attach", action="store_true")
    d.add_argument("--keep-open", action="store_true")
    d.add_argument("--out", help="evidence base dir (default runs/)")

    r = sub.add_parser("replay")
    r.add_argument("capability")
    r.add_argument("--param", "-p", action="append", help="name=value")
    r.add_argument("--tenant", default="heritage")
    r.add_argument("--fault", action="append", help="inject: slow_ms=4000, popup=true, session_expired=true, ...")
    r.add_argument("--approve-irreversible", action="store_true", help="caller pre-grants irreversible steps")
    r.add_argument("--allow-draft", action="store_true")
    r.add_argument("--attach", action="store_true")
    r.add_argument("--keep-open", action="store_true")
    r.add_argument("--operator", choices=["viewer01", "teller01"], help="sign on as this fake operator")
    r.add_argument("--handoff-timeout", type=float, default=900)
    r.add_argument("--out")

    sub.add_parser("catalog")
    ap_ = sub.add_parser("approve")
    ap_.add_argument("capability")
    ap_.add_argument("--reviewer", required=True)

    o = sub.add_parser("operator")
    o.add_argument("verb", choices=["list", "claim", "resolve", "show"])
    o.add_argument("id", nargs="?")
    o.add_argument("decision", nargs="?")
    o.add_argument("--as", dest="who", default="operator")
    o.add_argument("--notes", default="")

    c = sub.add_parser("console")
    c.add_argument("--port", type=int, default=8765)

    sub.add_parser("schema")

    args = ap.parse_args(argv)

    if args.cmd == "app":
        from .surface.swing import launch_corelink
        from .runtime import FAULTS, write_faults
        write_faults({})
        proc, _ = launch_corelink(tenant=args.tenant, port=args.port, faults_file=FAULTS)
        print(f"CoreLink ({args.tenant}) running with bridge on :{args.port}. Ctrl+C to stop.")
        try:
            proc.wait()
        except KeyboardInterrupt:
            proc.kill()
        return 0

    if args.cmd == "discover":
        from .runtime import app_session, run_discovery, write_faults
        write_faults({})
        with app_session(args.tenant, attach=args.attach, keep_open=args.keep_open) as s:
            res, cap, path = run_discovery(args.task, s, args.tenant, args.planner, args.script, args.model,
                                           out_dir=Path(args.out) if args.out else None)
        print(json.dumps({"run_id": res.run_id, "success": res.success, "summary": res.summary,
                          "steps": len(res.trace), "evidence": str(res.evidence.dir),
                          "capability": str(path) if path else None, "usage": res.usage}, indent=2))
        return 0 if res.success else 1

    if args.cmd == "replay":
        import os
        from .runtime import app_session, load_capability, run_replay, write_faults
        cap = load_capability(args.capability)
        if args.operator:
            os.environ["CORELINK_OPERATOR_ID"] = args.operator
        write_faults(_kv(args.fault))
        with app_session(args.tenant, attach=args.attach, keep_open=args.keep_open) as s:
            res = run_replay(cap, _kv(args.param), s, args.tenant, args.approve_irreversible, args.allow_draft,
                             args.handoff_timeout, out_dir=Path(args.out) if args.out else None)
        write_faults({})
        print(res.model_dump_json(indent=2, exclude_none=True))
        return {"success": 0, "business_outcome": 0}.get(res.status, 1)

    if args.cmd == "catalog":
        from .agent.compile import load_capability
        for p in sorted((ROOT / "capabilities").glob("*.json")):
            print(json.dumps(load_capability(str(p)).summary(), indent=2))
        return 0

    if args.cmd == "approve":
        from .agent.compile import load_capability, save_capability
        from datetime import datetime, timezone
        cap = load_capability(args.capability)
        cap.status = "approved"
        cap.provenance.reviewed_by = args.reviewer
        cap.provenance.notes.append(f"approved by {args.reviewer} at {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
        print(save_capability(cap))
        return 0

    if args.cmd == "operator":
        from .handoff import InterventionStore
        st = InterventionStore()
        if args.verb == "list":
            for rec in st.list():
                print(f"{rec['id']}  {rec['status']:8}  {rec['kind']:10}  {rec['reason']}")
        elif args.verb == "show":
            print(json.dumps(st.get(args.id), indent=2))
        elif args.verb == "claim":
            print(json.dumps(st.claim(args.id, args.who), indent=2))
            print("You now control the CoreLink window. Resolve when done.")
        elif args.verb == "resolve":
            print(json.dumps(st.resolve(args.id, args.decision, args.notes), indent=2))
        return 0

    if args.cmd == "console":
        import uvicorn
        from .console import app
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
        return 0

    if args.cmd == "schema":
        from .artifact import Capability, RunResult
        out = ROOT / "schemas"
        out.mkdir(exist_ok=True)
        (out / "capability.schema.json").write_text(json.dumps(Capability.model_json_schema(), indent=2))
        (out / "run_result.schema.json").write_text(json.dumps(RunResult.model_json_schema(), indent=2))
        print(f"wrote {out}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
