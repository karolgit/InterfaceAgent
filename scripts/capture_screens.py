"""Capture README screenshots of the CoreLink app through the bridge's offscreen renderer.

    python scripts/capture_screens.py  ->  docs/screenshots/*.png
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cua.runtime import FAULTS, write_faults  # noqa: E402
from cua.surface.swing import launch_corelink  # noqa: E402

OUT = ROOT / "docs" / "screenshots"


def shot(s, name):
    png, _ = s.screenshot()
    (OUT / name).write_bytes(png)
    print("saved", name)


def find(s, **kw):
    o = s.observe()
    for n in o.nodes():
        if all((getattr(n, k) or "") == v if not k.endswith("_has") else v in (getattr(n, k[:-4]) or "")
               for k, v in kw.items()):
            return n
    raise LookupError(kw)


def sign_on(s):
    s.act("set_text", ref=find(s, role="text", label="Operator ID:").ref, text="teller01")
    s.act("set_text", ref=find(s, role="password text").ref, text="demo123")
    s.act("click", ref=find(s, role="push button", name="Sign On").ref)
    s.settle()


def inquiry(s, member, button="Inquire"):
    s.act("key", key="F2")
    s.settle()
    f = [n for n in s.observe().nodes() if n.container == "Member Inquiry" and n.role == "text"][-1]
    s.act("set_text", ref=f.ref, text=member)
    s.act("click", ref=find(s, role="push button", name=button).ref)
    s.settle()


def fill_open(s, member, kind, amount, src):
    s.act("key", key="F5")
    s.settle()
    s.act("set_text", ref=find(s, role="text", label="Member Number").ref, text=member)
    s.act("click", ref=find(s, role="push button", name="Load").ref)
    s.settle()
    s.act("select", ref=find(s, role="combo box", label="Account Type").ref, option=kind)
    s.act("set_text", ref=find(s, role="text", label="Initial Deposit").ref, text=amount)
    s.act("select", ref=find(s, role="combo box", label="Fund From").ref, option=src)
    s.settle()


def close_frames(s):
    for n in s.observe().nodes():
        if n.role == "internal frame":
            s.act("close", ref=n.ref)
    s.settle()


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    write_faults({})
    proc, s = launch_corelink("heritage", faults_file=FAULTS)
    try:
        time.sleep(0.5)
        shot(s, "01-corelink-sign-on.png")
        sign_on(s)
        inquiry(s, "12345")
        shot(s, "02-member-inquiry.png")
        close_frames(s)
        inquiry(s, "99999")
        shot(s, "03-member-not-found.png")
        close_frames(s)
        fill_open(s, "12345", "Share Certificate", "1000.00", "S01")
        shot(s, "04-open-sub-account-form.png")
        s.act("click", ref=find(s, role="push button", name="Review >>").ref)
        s.settle()
        shot(s, "05-review-transaction.png")
        s.act("click", ref=find(s, role="push button", name="Post Transaction").ref)
        s.settle()
        shot(s, "06-confirm-posting-dialog.png")
        s.act("click", ref=find(s, role="push button", name="Yes").ref)
        s.settle()
        shot(s, "07-transaction-posted.png")
        close_frames(s)
        fill_open(s, "23456", "Money Market", "12000.00", "S10")
        s.act("click", ref=find(s, role="push button", name="Review >>").ref)
        s.settle()
        shot(s, "08-supervisor-override.png")
        s.act("click", ref=find(s, role="push button", name="Cancel", window="Supervisor Override Required").ref)
        s.settle()
    finally:
        proc.kill()
    time.sleep(1)
    proc, s = launch_corelink("lakeshore", faults_file=FAULTS)
    try:
        time.sleep(0.5)
        sign_on(s)
        inquiry(s, "23456", button="Search")
        shot(s, "09-second-tenant-lakeshore.png")
    finally:
        proc.kill()


if __name__ == "__main__":
    main()
