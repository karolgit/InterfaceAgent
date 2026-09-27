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
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cua import console as console_mod  # noqa: E402
from cua.gateway import CapabilityGateway, tool_name  # noqa: E402

# The chat assistant only routes requests to capabilities, so a cheaper model is enough here.
# Discovery (CUA_MODEL) stays on the strongest model.
MODEL = os.environ.get("PORTAL_MODEL", "claude-sonnet-5")
OFFLINE = os.environ.get("PORTAL_OFFLINE", "").lower() in ("1", "true", "yes")
DEMO_MEMBERS = {"12345": "Jane Sample", "23456": "Robert Testperson", "34567": "Alex Demo", "99999": "Unknown Member"}

SYSTEM = """You are the virtual assistant of Heritage Federal Credit Union, helping a signed-in member with \
their own accounts. You can only act through the tools provided; each tool runs a verified automation \
against the credit union's core system. The member's identity is already established; never ask for or \
accept a member number, and never act on anyone else's account.

Be brief and warm; answers may be read aloud, so avoid tables and markdown. State amounts plainly. If a \
tool returns a business outcome (for example MEMBER_RESTRICTED or VALIDATION_ERROR), explain it in plain \
words and suggest the next step. INVALID_OPTION includes the valid choices in "available"; offer those. \
If a tool fails, apologize briefly and offer to connect them with staff. \
Before opening an account, call the tool that lists open-account options: it returns the account types THIS \
credit union offers and the member's own shares that can fund one. Offer only those (the app shows them as \
buttons); never suggest other products, and never invent a share suffix. \
For anything irreversible (opening an account, moving money): as soon as you know every input, call the tool \
right away. Do NOT ask the member to confirm in chat first. Calling the tool does not execute anything; it \
makes the app show the member a Confirm button, which is the one and only confirmation step. Then describe \
in one or two sentences what will happen and ask them to press Confirm. Never claim it is done until you \
receive the result."""

SYSTEM_STAFF = """You are the internal service assistant of Heritage Federal Credit Union, helping a signed-in \
employee (contact-center staff) who serves many members. You can only act through the tools provided; each \
tool runs a verified automation against the credit union's core system. Staff may look up any member: every \
tool call needs the 5-digit member_number. If the employee has not said which member, ask for the member \
number; never guess one. Reuse the member number from earlier in the conversation when the employee clearly \
means the same member.

Be brief and factual; answers may be read aloud, so avoid tables and markdown. Say which member you looked up. \
Explain business outcomes (for example MEMBER_NOT_FOUND or SHARE_NOT_FOUND) in plain words. Before opening \
an account, call the open-account options tool for that member and offer only the account types and funding \
shares it returns. For anything \
irreversible: as soon as you know every input, call the tool right away without asking for confirmation in \
chat. The call only makes the app show a Confirm button, which is the one and only confirmation step. Then ask \
the employee to press Confirm, and never claim it is done until you receive the result."""

STAFF_USERS = {"sarah": "Sarah (Contact Center)"}

app = FastAPI(title="Member Assistant")
OPTIONS_TOOL = tool_name("corelink.account.get_open_options")
OPEN_TOOL = tool_name("corelink.account.open_share_sub_account")
BALANCE_TOOL = tool_name("corelink.member.get_share_balance")


@app.get("/console")
def console_redirect():
    return RedirectResponse("/console/")


# The operator console is served by the portal too, watching the portal's own CoreLink session.
app.mount("/console", console_mod.app)
gateway: CapabilityGateway | None = None
SESSIONS: dict[str, dict[str, Any]] = {}


def llm_available() -> bool:
    """AI mode needs a key and must not be switched off. Offline mode costs nothing."""
    if OFFLINE:
        return False
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


class Login(BaseModel):
    role: str = "member"  # member | staff
    member_number: str | None = None
    staff_id: str | None = None


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
            "members": DEMO_MEMBERS, "staff": STAFF_USERS, "capabilities": [{"id": c.id, "title": c.title, "status": c.status,
                                                       "risk": c.risk} for c in caps],
            "draft_mode": bool(gateway and gateway.allow_draft)}


@app.post("/api/login")
def login(body: Login):
    """Mock sign-in. A real deployment authenticates the member or employee before this point."""
    sid = uuid.uuid4().hex
    if body.role == "staff":
        name = STAFF_USERS.get((body.staff_id or "").lower())
        if not name:
            raise HTTPException(400, "unknown demo staff user")
        SESSIONS[sid] = {"role": "staff", "staff_id": body.staff_id.lower(), "name": name, "member_number": None,
                         "last_member": None, "messages": []}
        return {"session": sid, "name": name, "role": "staff"}
    num = (body.member_number or "").strip()
    if not re.fullmatch(r"\d{5}", num):
        raise HTTPException(400, "member ID must be 5 digits")
    name = DEMO_MEMBERS.get(num, f"Member {num}")
    SESSIONS[sid] = {"role": "member", "member_number": num, "name": name, "messages": []}
    return {"session": sid, "name": name, "role": "member"}


def _is_staff(sess: dict[str, Any]) -> bool:
    return sess.get("role") == "staff"


def _bound(sess: dict[str, Any]) -> dict[str, Any]:
    """Members are locked to their own number. Staff are authorized to choose the member per request."""
    return {} if _is_staff(sess) else {"member_number": sess["member_number"]}


def _requester(sess: dict[str, Any]) -> str:
    return f"staff:{sess['staff_id']}" if _is_staff(sess) else f"member:{sess['member_number']}"


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
        note = f"The {'employee' if _is_staff(sess) else 'member'} declined the pending action. Nothing was changed."
        result = {"status": "cancelled"}
    else:
        result = gateway.confirm(body.pending_id)
        note = f"The {'employee' if _is_staff(sess) else 'member'} pressed Confirm. Result: {json.dumps(result)}"
    if llm_available():
        reply = _llm_turn(sess, [{"type": "text", "text": f"(App notice, not typed by the member) {note}"}])["text"]
    else:
        reply = _phrase(result, "open", sess)
    return {"reply": reply, "result": result}


# ----------------------------------------------------------------------------- LLM path

def _llm_turn(sess: dict[str, Any], user_content: list[dict[str, Any]]) -> dict[str, Any]:
    import anthropic

    client = anthropic.Anthropic()
    msgs = sess["messages"]
    msgs.append({"role": "user", "content": user_content})
    tools = gateway.tools(bound=set() if _is_staff(sess) else {"member_number"})
    system = (SYSTEM_STAFF + f"\nThe signed-in employee is {sess['name']}." if _is_staff(sess)
              else SYSTEM + f"\nThe signed-in member's name is {sess['name']}.")
    choices = None
    for _ in range(6):
        resp = client.messages.create(model=MODEL, max_tokens=4000,
                                      system=system,
                                      tools=tools, messages=msgs, thinking={"type": "adaptive"},
                                      output_config={"effort": "low"})
        if resp.stop_reason == "refusal":
            return {"text": "Sorry, I can't help with that here. A staff member can assist you.", "choices": choices}
        msgs.append({"role": "assistant", "content": [b.model_dump(exclude_none=True) for b in resp.content]})
        calls = [b for b in resp.content if b.type == "tool_use"]
        if not calls:
            return {"text": " ".join(b.text for b in resp.content if b.type == "text").strip(), "choices": choices}
        results, pending = [], None
        for c in calls:
            try:
                r = gateway.invoke(c.name, dict(c.input), _bound(sess), requested_by=_requester(sess))
            except KeyError:
                r = {"status": "rejected", "failure": {"code": "UNKNOWN_CAPABILITY"}}
            if r.get("status") == "needs_confirmation":
                pending = r
            if c.name == OPTIONS_TOOL and r.get("status") == "success":
                choices = r.get("outputs")
            results.append({"type": "tool_result", "tool_use_id": c.id, "content": json.dumps(r)})
        msgs.append({"role": "user", "content": results})
        if pending:
            # Let the model tell the member what will happen, then stop and wait for the Confirm button.
            resp = client.messages.create(model=MODEL, max_tokens=1000, system=system, tools=tools, messages=msgs,
                                          thinking={"type": "adaptive"}, output_config={"effort": "low"})
            msgs.append({"role": "assistant", "content": [b.model_dump(exclude_none=True) for b in resp.content]})
            text = " ".join(b.text for b in resp.content if b.type == "text").strip()
            return {"text": text or "Please confirm to continue.", "pending": pending, "choices": None}
    return {"text": "Sorry, that took too many steps. Please try again.", "choices": choices}


def _chat_llm(sess: dict[str, Any], text: str) -> dict[str, Any]:
    out = _llm_turn(sess, [{"type": "text", "text": text}])
    return {"reply": out["text"], "pending": out.get("pending"), "choices": out.get("choices")}


# ----------------------------------------------------------------------------- offline fallback

def _money(s: str) -> str | None:
    m = re.search(r"\$?\s*([\d,]+(?:\.\d{1,2})?)", s)
    if not m:
        return None
    v = m.group(1).replace(",", "")
    return v if "." in v else v + ".00"


def _staff_member(sess: dict[str, Any], text: str) -> str | None:
    """Staff name the member in the request ("... for 12345"); otherwise reuse the last member discussed."""
    m = re.search(r"\b(\d{5})\b", text)
    if m:
        sess["last_member"] = m.group(1)
    return sess.get("last_member")


def _pick_fund(funds: list[str], text: str) -> str | None:
    t = text.lower()
    for f in funds:  # "S01 Share Savings": match the suffix, the full name, or a clear keyword
        suffix, _, desc = f.partition(" ")
        if suffix.lower() in t.split() or desc.lower() in t:
            return suffix
    for word in ("checking", "savings", "money market"):
        hits = [f for f in funds if word in f.lower()]
        if word in t and len(hits) == 1:
            return hits[0].partition(" ")[0]
    return None


def _open_flow(sess: dict[str, Any], args: dict[str, Any], text: str, asked_options: bool) -> dict[str, Any]:
    """Guided account opening: read valid options from the core system, collect type, amount, and funding share."""
    r = gateway.invoke(OPTIONS_TOOL, dict(args), _bound(sess), _requester(sess))
    if r.get("status") != "success":
        sess.pop("open_draft", None)
        return {"reply": _phrase(r, "open", sess), "result": r}
    types, funds = r["outputs"]["account_types"], r["outputs"]["fund_from"]
    draft = sess.setdefault("open_draft", {})
    t = text.lower()
    for ty in types:
        if ty.lower() in t:
            draft["account_type"] = ty
    known = next((i.enum for i in gateway.by_tool(OPEN_TOOL).inputs if i.name == "account_type"), None) or []
    t_no_products = t
    for k in known:  # "Youth Savings" must not be read as "fund it from savings"
        t_no_products = t_no_products.replace(k.lower(), " ")
    fund = _pick_fund(funds, t_no_products)
    if fund:
        draft["fund_from"] = fund
    cleaned = re.sub(r"\bs\d{2}\b", " ", t)
    if args.get("member_number"):
        cleaned = cleaned.replace(args["member_number"], " ")
    amt = _money(cleaned)
    if amt and float(amt) > 0:
        draft["initial_deposit"] = amt
    whose = f"member {args['member_number']}" if _is_staff(sess) else "you"
    not_offered = [k for k in known if k.lower() in t and k not in types]
    if not_offered:
        return {"reply": f"{not_offered[0]} isn't offered at this credit union. {whose.capitalize()} can open: "
                         f"{', '.join(types)}. Which one would you like?",
                "choices": {"account_types": types}}
    if asked_options and not draft:
        return {"reply": f"Here's what {whose} can open at this credit union: {', '.join(types)}. It can be funded "
                         f"from: {', '.join(funds)}. Tap a choice, or tell me the type, amount, and account.",
                "choices": {"account_types": types, "fund_from": funds}}
    missing = [k for k in ("account_type", "initial_deposit", "fund_from") if k not in draft]
    if missing:
        ask = {"account_type": "which account type", "initial_deposit": "the opening deposit amount",
               "fund_from": "which account to fund it from"}
        choices = {}
        if "account_type" in missing:
            choices["account_types"] = types
        if "fund_from" in missing:
            choices["fund_from"] = funds
        return {"reply": "Got it. I still need " + " and ".join(ask[m] for m in missing) + ".",
                "choices": choices or None}
    sess.pop("open_draft", None)
    r = gateway.invoke(OPEN_TOOL, {**args, **draft}, _bound(sess), _requester(sess))
    if r.get("status") == "needs_confirmation":
        src = next((f for f in funds if f.startswith(draft["fund_from"])), draft["fund_from"])
        return {"reply": f"I can open a {draft['account_type']} with ${draft['initial_deposit']} from {src}. "
                         "Please press Confirm to go ahead.", "pending": r}
    return {"reply": _phrase(r, "open", sess), "result": r}


def _chat_offline(sess: dict[str, Any], text: str) -> dict[str, Any]:
    t = text.lower()
    args: dict[str, Any] = {}
    if _is_staff(sess):
        member = _staff_member(sess, text)
        if member is None:
            return {"reply": "Which member? Please give me the 5-digit member number."}
        args["member_number"] = member
    asked_options = any(k in t for k in ("what account", "which account", "can i open", "can be opened",
                                          "options", "what can", "accounts can"))
    wants_open = asked_options or "open" in t or "fund it" in t or "open_draft" in sess
    if ("balance" in t or "how much" in t) and "open" not in t:
        share = ("Share Draft Checking" if "checking" in t or "draft" in t else
                 "Money Market" if "money market" in t else "Share Savings")
        r = gateway.invoke(BALANCE_TOOL, {**args, "share_type": share}, _bound(sess), _requester(sess))
        r["share_type"] = share
        r["member"] = args.get("member_number")
        return {"reply": _phrase(r, "balance", sess), "result": r}
    if wants_open:
        return _open_flow(sess, args, text, asked_options)
    if _is_staff(sess):
        return {"reply": f"I'm on member {args['member_number']}. I can read a share balance or help open an "
                         "account. Ask \"what accounts can this member open?\" to see the options."}
    return {"reply": "I can check your savings or checking balance, or help you open an account. Try \"What "
                     "accounts can I open?\""}


def _phrase(r: dict[str, Any], intent: str, sess: dict[str, Any] | None = None) -> str:
    st = r.get("status")
    staff = bool(sess and _is_staff(sess))
    who = f"Member {r.get('member') or (sess or {}).get('last_member')}'s" if staff else "Your"
    if st == "cancelled":
        return "No problem, I didn't make any changes."
    if st == "success" and intent == "balance":
        v = r["outputs"].get("balance", r["outputs"].get("savings_balance"))
        return f"{who} {r.get('share_type', 'Share Savings')} balance is ${float(v):,.2f}."
    if st == "success":
        return f"Done. The new account is open. Confirmation number {r['outputs'].get('confirmation_number')}."
    if st == "business_outcome":
        code = r["outcome"]["code"]
        return {"MEMBER_RESTRICTED": "Your account has a restriction, so I can't do that online. Please contact a branch.",
                "MEMBER_NOT_FOUND": ("There's no member with that number." if staff else
                                     "I couldn't find your membership record. Please contact us."),
                "VALIDATION_ERROR": f"The system didn't accept that: {r['outcome'].get('ui_text', '')}.",
                "PERMISSION_DENIED": "That service isn't available right now.",
                "INVALID_OPTION": ("That choice isn't available here. Options: "
                                   + ", ".join(r["outcome"].get("available") or []) + "."),
                "SHARE_NOT_FOUND": ("That member has no account of that type." if staff else
                                    "I don't see an account of that type on your membership."),
                }.get(code, f"I couldn't complete that ({code}).")
    return "Sorry, something went wrong on our side. A staff member will follow up."


def main() -> None:
    global gateway
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8800)
    ap.add_argument("--tenant", default="heritage")
    ap.add_argument("--offline", action="store_true",
                    help="no LLM calls (free): a keyword router picks capabilities, even if an API key is set")
    ap.add_argument("--allow-draft", action="store_true",
                    help="serve DRAFT capabilities (supervised demo only; production requires approved)")
    a = ap.parse_args()
    global OFFLINE
    OFFLINE = OFFLINE or a.offline
    gateway = CapabilityGateway(tenant=a.tenant, allow_draft=a.allow_draft,
                                port=int(os.environ.get("CUA_GATEWAY_PORT", "8741")))
    console_mod.BRIDGE = f"http://127.0.0.1:{gateway.port}"
    print(f"Member Assistant on http://127.0.0.1:{a.port}  mode={'llm' if llm_available() else 'offline'}")
    try:
        uvicorn.run(app, host="127.0.0.1", port=a.port, log_level="warning")
    finally:
        gateway.shutdown()


if __name__ == "__main__":
    main()
