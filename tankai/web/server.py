#!/usr/bin/env python3
"""
TankAI Web-UI

Start:
  python -m tankai.web.server

Env:
  TANKAI_HOST=127.0.0.1
  TANKAI_PORT=8765
  TANKAI_BASIC_AUTH_USER / TANKAI_BASIC_AUTH_PASS
  TANKAI_LLM, OPENAI_API_KEY, ...
  TANKAI_RUN_STORE=tankai_runs.jsonl
"""

from __future__ import annotations

import base64
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tankai import TankAI, get_llm
from tankai.core.long_term_memory import LongTermMemory

_tank: TankAI | None = None
_lock = threading.Lock()
_history: list[dict] = []


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def get_tank() -> TankAI:
    global _tank
    if _tank is None:
        run_store = _env("TANKAI_RUN_STORE", "tankai_runs.jsonl") or None
        try:
            llm = get_llm()
        except Exception as e:
            print(f"[web] LLM-Init fehlgeschlagen ({e}), nutze Mock")
            llm = get_llm("mock")
        _tank = TankAI(
            llm=llm,
            verbose=False,
            use_ltm=True,
            enable_tools=True,
            parallel=False,
            run_store_path=run_store,
        )
        try:
            _tank.ltm = LongTermMemory(
                in_memory=_env("TANKAI_LTM_MEMORY", "1") in ("1", "true", "yes"),
                embedder=_env("TANKAI_EMBEDDER", "hashing"),
            )
        except Exception:
            _tank.ltm = LongTermMemory(in_memory=True, embedder="hashing")
        _tank.tools.register_defaults(ltm=_tank.ltm)
        print(f"[web] LLM={type(llm).__name__} | {_tank.ltm.summary()}")
    return _tank


def _check_auth(handler: BaseHTTPRequestHandler) -> bool:
    user = _env("TANKAI_BASIC_AUTH_USER")
    pw = _env("TANKAI_BASIC_AUTH_PASS")
    if not user or not pw:
        return True  # Auth deaktiviert
    header = handler.headers.get("Authorization", "")
    if not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:]).decode("utf-8")
        u, p = decoded.split(":", 1)
        return u == user and p == pw
    except Exception:
        return False


HTML = r"""<!DOCTYPE html>
<html lang="de">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>TankAI — Web Intelligence OS</title>
  <style>
    :root {
      --bg:#0b0f14; --card:#1a2332; --border:#243044;
      --accent:#3b82f6; --accent2:#8b5cf6; --text:#e8eef7; --muted:#8b9bb4;
    }
    * { box-sizing: border-box; }
    body { margin:0; font-family: system-ui, sans-serif; background:var(--bg); color:var(--text); }
    header {
      padding:1rem 1.5rem; border-bottom:1px solid var(--border);
      display:flex; justify-content:space-between; align-items:center;
      background: linear-gradient(90deg, #0f172a, #1e1b4b);
    }
    header h1 { margin:0; font-size:1.15rem; }
    .ver { color:var(--muted); font-size:0.8rem; }
    .layout { display:grid; grid-template-columns:1fr 300px; gap:1rem; max-width:1100px; margin:0 auto; padding:1rem; }
    @media (max-width:900px){ .layout{ grid-template-columns:1fr; } }
    .card { background:var(--card); border:1px solid var(--border); border-radius:12px; padding:1.1rem; margin-bottom:1rem; }
    .card h2 { margin:0 0 .7rem; font-size:.85rem; color:var(--muted); text-transform:uppercase; letter-spacing:.04em; }
    label { display:block; font-size:.8rem; color:var(--muted); margin-bottom:.3rem; }
    textarea,input { width:100%; background:var(--bg); border:1px solid var(--border); border-radius:8px;
      color:var(--text); padding:.65rem .75rem; margin-bottom:.7rem; font-size:.92rem; }
    textarea { min-height:100px; resize:vertical; }
    button { background:linear-gradient(135deg,var(--accent),var(--accent2)); color:#fff; border:none;
      border-radius:8px; padding:.7rem 1.3rem; font-weight:600; cursor:pointer; }
    button:disabled { opacity:.45; cursor:not-allowed; }
    .badge { display:inline-block; padding:.15rem .5rem; border-radius:999px; font-size:.72rem; font-weight:600; margin:0 .25rem .25rem 0; }
    .b-ok { background:#14532d; color:#86efac; } .b-err { background:#7f1d1d; color:#fca5a5; }
    .b-info { background:#1e3a5f; color:#93c5fd; }
    #answer { white-space:pre-wrap; line-height:1.55; }
    table { width:100%; border-collapse:collapse; font-size:.85rem; }
    th,td { text-align:left; padding:.4rem .5rem; border-bottom:1px solid var(--border); }
    th { color:var(--muted); font-weight:500; }
    .hist-item { padding:.5rem 0; border-bottom:1px solid var(--border); cursor:pointer; font-size:.85rem; }
    .hist-item:hover { color:var(--accent); }
    #status { color:var(--muted); font-size:.85rem; }
  </style>
</head>
<body>
  <header>
    <div><h1>TankAI</h1><div class="ver">Web Intelligence OS</div></div>
    <div class="ver" id="health">…</div>
  </header>
  <div class="layout">
    <div>
      <div class="card">
        <h2>Neues Ziel</h2>
        <label>Ziel</label>
        <textarea id="goal" placeholder="Was soll erreicht werden?"></textarea>
        <label>Definition of Done</label>
        <input id="dod" value="Eine klare, überprüfbare Antwort liegt vor."/>
        <div style="margin-bottom:.7rem">
          <label style="display:inline"><input type="checkbox" id="parallel"/> Parallel</label>
        </div>
        <button id="runBtn" onclick="runTank()">TankAI starten</button>
        <span id="status"></span>
      </div>
      <div class="card" id="outCard" style="display:none">
        <div id="badges"></div>
        <h2 style="margin-top:.75rem">Antwort</h2>
        <div id="answer"></div>
      </div>
      <div class="card" id="planCard" style="display:none">
        <h2>Plan</h2>
        <div id="rationale" class="ver" style="margin-bottom:.5rem"></div>
        <table><thead><tr><th>#</th><th>Typ</th><th>Beschreibung</th><th>Status</th></tr></thead>
        <tbody id="planBody"></tbody></table>
      </div>
      <div class="card" id="receiptCard" style="display:none">
        <h2>Receipts</h2>
        <table><thead><tr><th>Actor</th><th>Action</th><th>OK</th><th>Summary</th></tr></thead>
        <tbody id="receiptBody"></tbody></table>
      </div>
    </div>
    <div>
      <div class="card"><h2>System</h2><div id="sysInfo" class="ver">—</div></div>
      <div class="card"><h2>Verlauf</h2><div id="history" class="ver">Noch keine Runs.</div></div>
    </div>
  </div>
<script>
async function refreshHealth(){
  try{
    const r=await fetch('/api/health'); const d=await r.json();
    document.getElementById('health').textContent=d.ok?'Online · '+d.version:'Offline';
    document.getElementById('sysInfo').textContent=(d.llm||'')+'\n'+(d.ltm||'');
  }catch{ document.getElementById('health').textContent='Offline'; }
}
async function loadHistory(){
  try{
    const r=await fetch('/api/history'); const d=await r.json();
    const el=document.getElementById('history');
    if(!d.length){ el.textContent='Noch keine Runs.'; return; }
    window.__hist=d;
    el.innerHTML=d.map((h,i)=>`<div class="hist-item" onclick="showStored(${i})">${(h.goal||'').slice(0,60)}</div>`).join('');
  }catch{}
}
function showStored(i){ if(window.__hist&&window.__hist[i]) renderResult(window.__hist[i]); }
function renderResult(data){
  document.getElementById('outCard').style.display='block';
  document.getElementById('answer').textContent=data.final_answer||'';
  const st=data.status==='completed'?'b-ok':'b-err';
  document.getElementById('badges').innerHTML=
    `<span class="badge ${st}">${data.status}</span>`+
    `<span class="badge b-info">${data.duration_seconds}s</span>`+
    `<span class="badge b-info">${data.receipts_count||0} Receipts</span>`;
  if(data.plan&&data.plan.steps){
    document.getElementById('planCard').style.display='block';
    document.getElementById('rationale').textContent=data.plan.rationale||'';
    document.getElementById('planBody').innerHTML=data.plan.steps.map((s,i)=>
      `<tr><td>${i+1}</td><td>${s.specialist_type}</td><td>${s.description}</td><td>${s.status||'—'}</td></tr>`).join('');
  }
  if(data.receipts&&data.receipts.length){
    document.getElementById('receiptCard').style.display='block';
    document.getElementById('receiptBody').innerHTML=data.receipts.map(r=>
      `<tr><td>${r.actor}</td><td>${r.action}</td><td>${r.success?'✓':'✗'}</td><td>${(r.output_summary||'').slice(0,80)}</td></tr>`).join('');
  }
  if(data.ltm) document.getElementById('sysInfo').textContent=data.ltm;
}
async function runTank(){
  const goal=document.getElementById('goal').value.trim();
  const dod=document.getElementById('dod').value.trim();
  if(!goal){ alert('Bitte ein Ziel eingeben'); return; }
  const btn=document.getElementById('runBtn'); btn.disabled=true;
  document.getElementById('status').textContent='PLAN → ROUTE → VERIFY → LEARN …';
  ['outCard','planCard','receiptCard'].forEach(id=>document.getElementById(id).style.display='none');
  try{
    const res=await fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({goal,definition_of_done:dod,parallel:document.getElementById('parallel').checked})});
    const data=await res.json();
    if(data.error) throw new Error(data.error);
    renderResult(data);
    document.getElementById('status').textContent='Fertig.';
    loadHistory();
  }catch(e){ document.getElementById('status').textContent='Fehler: '+e.message; }
  finally{ btn.disabled=false; }
}
refreshHealth(); loadHistory(); setInterval(refreshHealth,15000);
</script>
</body>
</html>
"""


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[web] {args[0]}")

    def _unauthorized(self):
        body = b"Unauthorized"
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="TankAI"')
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _require_auth(self) -> bool:
        if _check_auth(self):
            return True
        self._unauthorized()
        return False

    def do_GET(self):
        if not self._require_auth():
            return
        if self.path in ("/", "/index.html"):
            body = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/health":
            tank = get_tank()
            self._json({
                "ok": True,
                "version": "0.5.1",
                "llm": type(tank.llm).__name__,
                "ltm": tank.ltm.summary() if tank.ltm else "",
            })
        elif self.path == "/api/history":
            self._json(_history[-20:][::-1])
        else:
            self.send_error(404)

    def do_POST(self):
        if not self._require_auth():
            return
        if self.path != "/api/run":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", 0))
        if length > 200_000:
            self._json({"error": "Payload zu groß"}, 413)
            return
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
            goal = (data.get("goal") or data.get("goal_description") or "").strip()
            dod = data.get("definition_of_done") or "Eine klare, überprüfbare Antwort liegt vor."
            if not goal:
                self._json({"error": "goal fehlt"}, 400)
                return
            if len(goal) > 20_000:
                self._json({"error": "goal zu lang"}, 400)
                return

            with _lock:
                tank = get_tank()
                tank.parallel = bool(data.get("parallel", False))
                result = tank.run(goal_description=goal, definition_of_done=dod)

            plan_data = None
            if result.plan:
                plan_data = {
                    "rationale": result.plan.rationale,
                    "steps": [
                        {
                            "specialist_type": s.specialist_type,
                            "description": s.description,
                            "status": s.status.value if hasattr(s.status, "value") else str(s.status),
                        }
                        for s in result.plan.steps
                    ],
                }
            receipts = [
                {
                    "actor": r.actor.value if hasattr(r.actor, "value") else str(r.actor),
                    "action": r.action,
                    "success": r.success,
                    "output_summary": r.output_summary,
                }
                for r in result.receipts
            ]
            payload = {
                "goal": goal,
                "final_answer": result.final_answer,
                "status": result.status.value if hasattr(result.status, "value") else str(result.status),
                "duration_seconds": result.duration_seconds,
                "receipts_count": len(result.receipts),
                "receipts": receipts,
                "plan": plan_data,
                "ltm": tank.ltm.summary() if tank.ltm else "",
            }
            _history.append(payload)
            if len(_history) > 100:
                del _history[:-50]
            self._json(payload)
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    host = _env("TANKAI_HOST", "127.0.0.1") or "127.0.0.1"
    port = int(_env("TANKAI_PORT", "8765") or "8765")
    # Preload tank so LLM errors surface at start
    get_tank()
    server = ThreadedHTTPServer((host, port), Handler)
    auth = "an" if _env("TANKAI_BASIC_AUTH_USER") else "aus"
    print(f"TankAI Web-UI → http://{host}:{port}  (Basic-Auth: {auth})")
    print("Ctrl+C zum Beenden")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()


if __name__ == "__main__":
    main()
