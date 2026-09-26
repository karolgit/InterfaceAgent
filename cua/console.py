"""Minimal operator console (the "mock operator UI" the brief allows).

Real parts: the intervention queue, claim/resolve, control-state display, and a live view of the
session the automation is using. The operator performs manual steps directly in the CoreLink window
(same process, same signed-on session), which the bridge unlocks and records while they hold control.
Cut: real-time co-browsing through the browser (remote input), auth, multi-operator routing.
"""
from __future__ import annotations

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel

from .handoff import InterventionStore

app = FastAPI(title="CUA Operator Console")
store = InterventionStore()
BRIDGE = f"http://127.0.0.1:{__import__('os').environ.get('CUA_PORT', '8740')}"


class Claim(BaseModel):
    operator: str


class Resolve(BaseModel):
    decision: str
    notes: str = ""


@app.get("/api/interventions")
def list_interventions():
    return store.list()


@app.post("/api/interventions/{iid}/claim")
def claim(iid: str, body: Claim):
    try:
        return store.claim(iid, body.operator)
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.post("/api/interventions/{iid}/resolve")
def resolve(iid: str, body: Resolve):
    try:
        return store.resolve(iid, body.decision, body.notes)
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.get("/shot/{iid}.png")
def shot(iid: str):
    p = store.base / f"{iid}.png"
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p)


@app.get("/api/session")
def session():
    try:
        return httpx.get(f"{BRIDGE}/health", timeout=1).json()
    except httpx.HTTPError:
        return {"ok": False, "mode": "offline"}


@app.get("/live.png")
def live():
    try:
        r = httpx.get(f"{BRIDGE}/screenshot", timeout=3)
        return Response(r.content, media_type="image/png", headers={"Cache-Control": "no-store"})
    except httpx.HTTPError:
        raise HTTPException(503, "no live session")


PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Operator Console</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{--bg:#f4f5f7;--card:#fff;--ink:#1d2330;--muted:#5d6475;--line:#d9dde5;--accent:#1f3a68;
--open:#b06a00;--claimed:#0b6b2e;--done:#5d6475}
@media (prefers-color-scheme: dark){:root{--bg:#14171d;--card:#1d2129;--ink:#e6e8ec;--muted:#9aa1ae;--line:#2d333e;--accent:#8fb0ea}}
body{margin:0;font:14px/1.45 system-ui,sans-serif;background:var(--bg);color:var(--ink)}
header{padding:14px 20px;border-bottom:1px solid var(--line);display:flex;gap:16px;align-items:center;flex-wrap:wrap}
h1{font-size:17px;margin:0}.pill{padding:3px 10px;border-radius:99px;font-weight:600;font-size:12px;color:#fff}
main{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:16px;padding:16px 20px}
@media (max-width:900px){main{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px;margin-bottom:12px}
.muted{color:var(--muted)} img{max-width:100%;border:1px solid var(--line);border-radius:4px}
button{font:inherit;padding:6px 12px;border-radius:6px;border:1px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer}
button.primary{background:var(--accent);color:#fff;border-color:var(--accent)}
input,textarea{font:inherit;padding:6px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--ink)}
code{font-size:12px}.row{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}
</style></head><body>
<header><h1>Operator Console</h1><span id="ctl" class="pill" style="background:#777">...</span>
<span class="muted">Claim a request, then work directly in the CoreLink window. Your actions are recorded.</span>
<label>Operator <input id="who" value="karol" size="10"></label></header>
<main><section><h2>Intervention requests</h2><div id="list"></div></section>
<section><h2>Live session</h2><div class="card"><img id="live" alt="live session view"></div></section></main>
<script>
const colors={agent:'#8b0000',human:'#0b6b2e',paused:'#b06a00',free:'#555',offline:'#555'};
async function j(u,o){const r=await fetch(u,o);if(!r.ok)alert(await r.text());return r.json()}
async function act(id,verb,decision){
  const who=document.getElementById('who').value||'operator';
  if(verb==='claim') await j(`/api/interventions/${id}/claim`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({operator:who})});
  else{const notes=document.getElementById('n-'+id).value;
    await j(`/api/interventions/${id}/resolve`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({decision,notes})});}
  refresh();}
function esc(s){return String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
async function refresh(){
  const s=await j('/api/session');const c=document.getElementById('ctl');
  c.textContent='session: '+(s.mode||'offline')+(s.holder?' ('+s.holder+')':'');c.style.background=colors[s.mode]||'#555';
  const items=await j('/api/interventions');const el=document.getElementById('list');
  el.innerHTML=items.slice(0,12).map(r=>{
    const live=r.status==='open'||r.status==='claimed';
    const btns=r.status==='open'?`<button class="primary" onclick="act('${r.id}','claim')">Take control</button>`:
      r.status==='claimed'?['resume','approve','deny','abort'].map(d=>`<button ${d==='resume'||d==='approve'?'class="primary"':''} onclick="act('${r.id}','resolve','${d}')">${d}</button>`).join(''):'';
    return `<div class="card"><b>${esc(r.kind)}</b> <span class="pill" style="background:var(--${r.status==='open'?'open':r.status==='claimed'?'claimed':'done'})">${esc(r.status)}</span>
      <div>${esc(r.reason)}</div><div class="muted"><code>${esc(r.id)}</code> run <code>${esc(r.run_id)}</code> ${r.operator?'operator '+esc(r.operator):''}</div>
      <details ${live?'open':''}><summary>context</summary><pre style="white-space:pre-wrap">${esc(JSON.stringify(r.context,null,1))}</pre>
      ${r.screenshot?`<img src="/shot/${r.id}.png" alt="screenshot at escalation (redacted)">`:''}</details>
      ${live?`<div class="row"><input id="n-${r.id}" placeholder="notes for the record" style="flex:1"></div><div class="row">${btns}</div>`:''}
      ${r.human_events?`<div class="muted">${r.human_events.filter(e=>e.actor==='human').length} human actions recorded</div>`:''}</div>`}).join('')||'<p class="muted">No requests.</p>';
  document.getElementById('live').src='/live.png?'+Date.now();}
refresh();setInterval(refresh,2000);
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE
