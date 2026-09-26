"""Member Assistant: the customer-facing app that exercises capabilities end to end.

Member (browser, text or voice) -> assistant (Claude + capability tools) -> CapabilityGateway ->
deterministic replay against the legacy CoreLink desktop app -> answer.

    python -m portal.app [--port 8800] [--tenant heritage] [--allow-draft]

Identity: the member "signs in" by picking a demo member. Their member number is bound server-side
and never offered to the model as a parameter.
Without ANTHROPIC_API_KEY the assistant falls back to a small offline intent router so the demo
still runs; the page labels which mode is active.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cua.gateway import CapabilityGateway, tool_name  # noqa: E402

MODEL = os.environ.get("PORTAL_MODEL", os.environ.get("CUA_MODEL", "claude-opus-5"))
DEMO_MEMBERS = {"12345": "Jane Sample", "23456": "Robert Testperson", "34567": "Alex Demo", "99999": "Unknown Member"}

SYSTEM = """You are the virtual assistant of Heritage Federal Credit Union, helping a signed-in member with \
their own accounts. You can only act through the tools provided; each tool runs a verified automation \
against the credit union's core system. The member's identity is already established; never ask for or \
accept a member number, and never act on anyone else's account.

Be brief and warm; answers may be read aloud, so avoid tables and markdown. State amounts plainly. If a \
tool returns a business outcome (for example MEMBER_RESTRICTED or VALIDATION_ERROR), explain it in plain \
words and suggest the next step. If a tool fails, apologize briefly and offer to connect them with staff. \
For anything irreversible, the app asks the member to press Confirm; describe what will happen and never \
claim it is done until you receive the result."""

app = FastAPI(title="Member Assistant")
gateway: CapabilityGateway | None = None
SESSIONS: dict[str, dict[str, Any]] = {}


def llm_available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


class Login(BaseModel):
    member_number: str


class Chat(BaseModel):
    session: str
    text: str


class Confirm(BaseModel):
    session: str
    pending_id: str
    accept: bool


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/api/info")
def info():
    caps = gateway.capabilities() if gateway else []
    return {"mode": "llm" if llm_available() else "offline", "model": MODEL if llm_available() else None,
            "members": DEMO_MEMBERS, "capabilities": [{"id": c.id, "title": c.title, "status": c.status,
                                                       "risk": c.risk} for c in caps],
            "draft_mode": bool(gateway and gateway.allow_draft)}


@app.post("/api/login")
def login(body: Login):
    if body.member_number not in DEMO_MEMBERS:
        raise HTTPException(400, "unknown demo member")
    sid = uuid.uuid4().hex
    SESSIONS[sid] = {"member_number": body.member_number, "name": DEMO_MEMBERS[body.member_number], "messages": []}
    return {"session": sid, "name": DEMO_MEMBERS[body.member_number]}


def _bound(sess: dict[str, Any]) -> dict[str, Any]:
    return {"member_number": sess["member_number"]}


@app.post("/api/chat")
def chat(body: Chat):
    sess = SESSIONS.get(body.session)
    if not sess:
        raise HTTPException(401, "sign in first")
    if llm_available():
        return _chat_llm(sess, body.text)
    return _chat_offline(sess, body.text)


@app.post("/api/confirm")
def confirm(body: Confirm):
    sess = SESSIONS.get(body.session)
    if not sess:
        raise HTTPException(401, "sign in first")
    if not body.accept:
        gateway.cancel(body.pending_id)
        note = "The member declined the pending action. Nothing was changed."
        result = {"status": "cancelled"}
    else:
        result = gateway.confirm(body.pending_id)
        note = f"The member pressed Confirm. Result: {json.dumps(result)}"
    if llm_available():
        reply = _llm_turn(sess, [{"type": "text", "text": f"(App notice, not typed by the member) {note}"}])
    else:
        reply = _phrase(result, "open")
    return {"reply": reply, "result": result}


# ----------------------------------------------------------------------------- LLM path

def _llm_turn(sess: dict[str, Any], user_content: list[dict[str, Any]]) -> str | dict[str, Any]:
    import anthropic

    client = anthropic.Anthropic()
    msgs = sess["messages"]
    msgs.append({"role": "user", "content": user_content})
    tools = gateway.tools(bound={"member_number"})
    for _ in range(6):
        resp = client.messages.create(model=MODEL, max_tokens=4000,
                                      system=SYSTEM + f"\nThe signed-in member's name is {sess['name']}.",
                                      tools=tools, messages=msgs, thinking={"type": "adaptive"},
                                      output_config={"effort": "low"})
        if resp.stop_reason == "refusal":
            return "Sorry, I can't help with that here. A staff member can assist you."
        msgs.append({"role": "assistant", "content": [b.model_dump(exclude_none=True) for b in resp.content]})
        calls = [b for b in resp.content if b.type == "tool_use"]
        if not calls:
            return " ".join(b.text for b in resp.content if b.type == "text").strip()
        results, pending = [], None
        for c in calls:
            try:
                r = gateway.invoke(c.name, dict(c.input), _bound(sess), requested_by=f"member:{sess['member_number']}")
            except KeyError:
                r = {"status": "rejected", "failure": {"code": "UNKNOWN_CAPABILITY"}}
            if r.get("status") == "needs_confirmation":
                pending = r
            results.append({"type": "tool_result", "tool_use_id": c.id, "content": json.dumps(r)})
        msgs.append({"role": "user", "content": results})
        if pending:
            # Let the model tell the member what will happen, then stop and wait for the Confirm button.
            resp = client.messages.create(model=MODEL, max_tokens=1000, system=SYSTEM, tools=tools, messages=msgs,
                                          thinking={"type": "adaptive"}, output_config={"effort": "low"})
            msgs.append({"role": "assistant", "content": [b.model_dump(exclude_none=True) for b in resp.content]})
            text = " ".join(b.text for b in resp.content if b.type == "text").strip()
            return {"text": text or "Please confirm to continue.", "pending": pending}
    return "Sorry, that took too many steps. Please try again."


def _chat_llm(sess: dict[str, Any], text: str) -> dict[str, Any]:
    out = _llm_turn(sess, [{"type": "text", "text": text}])
    if isinstance(out, dict):
        return {"reply": out["text"], "pending": out["pending"]}
    return {"reply": out}


# ----------------------------------------------------------------------------- offline fallback

def _money(s: str) -> str | None:
    m = re.search(r"\$?\s*([\d,]+(?:\.\d{1,2})?)", s)
    if not m:
        return None
    v = m.group(1).replace(",", "")
    return v if "." in v else v + ".00"


def _chat_offline(sess: dict[str, Any], text: str) -> dict[str, Any]:
    t = text.lower()
    if "balance" in t or "how much" in t:
        share = ("Share Draft Checking" if "checking" in t or "draft" in t else
                 "Money Market" if "money market" in t else "Share Savings")
        r = gateway.invoke(tool_name("corelink.member.get_share_balance"), {"share_type": share}, _bound(sess),
                           "member")
        r["share_type"] = share
        return {"reply": _phrase(r, "balance"), "result": r}
    if "open" in t or "certificate" in t or "money market" in t or "club" in t:
        kind = ("Money Market" if "money market" in t else "Christmas Club" if "club" in t else "Share Certificate")
        amt = _money(t) or "500.00"
        src = "S10" if "checking" in t else "S01"
        r = gateway.invoke(tool_name("corelink.account.open_share_sub_account"),
                           {"account_type": kind, "initial_deposit": amt, "fund_from": src}, _bound(sess), "member")
        if r.get("status") == "needs_confirmation":
            return {"reply": f"I can open a {kind} with ${amt} from your "
                             f"{'checking' if src == 'S10' else 'savings'}. Please press Confirm to go ahead.",
                    "pending": r}
        return {"reply": _phrase(r, "open"), "result": r}
    return {"reply": "I can check your savings balance or open a certificate, money market, or club account. "
                     "What would you like to do?"}


def _phrase(r: dict[str, Any], intent: str) -> str:
    st = r.get("status")
    if st == "cancelled":
        return "No problem, I didn't make any changes."
    if st == "success" and intent == "balance":
        v = r["outputs"].get("balance", r["outputs"].get("savings_balance"))
        return f"Your {r.get('share_type', 'Share Savings')} balance is ${float(v):,.2f}."
    if st == "success":
        return f"Done. Your new account is open. Confirmation number {r['outputs'].get('confirmation_number')}."
    if st == "business_outcome":
        code = r["outcome"]["code"]
        return {"MEMBER_RESTRICTED": "Your account has a restriction, so I can't do that online. Please contact a branch.",
                "MEMBER_NOT_FOUND": "I couldn't find your membership record. Please contact us.",
                "VALIDATION_ERROR": f"The system didn't accept that: {r['outcome'].get('ui_text', '')}.",
                "PERMISSION_DENIED": "That service isn't available right now.",
                "SHARE_NOT_FOUND": "I don't see an account of that type on your membership.",
                }.get(code, f"I couldn't complete that ({code}).")
    return "Sorry, something went wrong on our side. A staff member will follow up."


def main() -> None:
    global gateway
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8800)
    ap.add_argument("--tenant", default="heritage")
    ap.add_argument("--allow-draft", action="store_true",
                    help="serve DRAFT capabilities (supervised demo only; production requires approved)")
    a = ap.parse_args()
    gateway = CapabilityGateway(tenant=a.tenant, allow_draft=a.allow_draft)
    print(f"Member Assistant on http://127.0.0.1:{a.port}  mode={'llm' if llm_available() else 'offline'}")
    try:
        uvicorn.run(app, host="127.0.0.1", port=a.port, log_level="warning")
    finally:
        gateway.shutdown()


if __name__ == "__main__":
    main()
