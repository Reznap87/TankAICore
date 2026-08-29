#!/usr/bin/env python3
"""Mandantenfähige TankAI-Weboberfläche mit serverseitigen Sessions."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from http import cookies
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Any

from pydantic import ValidationError as PydanticValidationError
from urllib.parse import urlsplit
from uuid import UUID

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tankai import __version__
from tankai.core.llm import LLMRateLimitExceeded
from tankai.dev_orchestrator.job_queue import DevelopmentJobQueue, QueueError
from tankai.dev_orchestrator.models import WorkerPipelineJob
from tankai.web.auth import (
    AGENT_TOKEN_SCOPES,
    AgentAuthContext,
    AuthContext,
    AuthStore,
    ProviderCallRateLimiter,
)
from tankai.web.runtime import WorkspaceRuntimeManager

_CSP_NONCE = secrets.token_urlsafe(18)
_SESSION_COOKIE = "tankai_session"
_LOCAL_TENANT_ID = "00000000-0000-4000-8000-000000000001"
_LOCAL_WORKSPACE_ID = "00000000-0000-4000-8000-000000000002"
_BRAND_ASSET_ROOT = Path(__file__).with_name("static")
_BRAND_ASSETS = {
    "/favicon.ico": ("image/x-icon", (_BRAND_ASSET_ROOT / "favicon.ico").read_bytes()),
    "/favicon.png": ("image/png", (_BRAND_ASSET_ROOT / "tankaicore-icon.png").read_bytes()),
    "/apple-touch-icon.png": (
        "image/png",
        (_BRAND_ASSET_ROOT / "apple-touch-icon.png").read_bytes(),
    ),
}


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_bool(name: str, default: bool = False) -> bool:
    return _env(name, "1" if default else "0").lower() in {"1", "true", "yes", "on"}


def _safe_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(_env(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} muss eine Ganzzahl sein") from exc
    return max(minimum, min(maximum, value))


class LoginRateLimiter:
    """Prozesslokale Schutzschicht zusätzlich zu Reverse-Proxy-Limits."""

    def __init__(self, *, limit: int = 5, window_seconds: int = 300, block_seconds: int = 900) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self.block_seconds = block_seconds
        self._lock = threading.RLock()
        self._attempts: dict[str, list[float]] = {}
        self._blocked_until: dict[str, float] = {}

    def allowed(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            blocked = self._blocked_until.get(key, 0.0)
            if blocked > now:
                return False
            cutoff = now - self.window_seconds
            current = [ts for ts in self._attempts.get(key, []) if ts >= cutoff]
            self._attempts[key] = current
            return len(current) < self.limit

    def failure(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            cutoff = now - self.window_seconds
            current = [ts for ts in self._attempts.get(key, []) if ts >= cutoff]
            current.append(now)
            self._attempts[key] = current
            if len(current) >= self.limit:
                self._blocked_until[key] = now + self.block_seconds

    def success(self, key: str) -> None:
        with self._lock:
            self._attempts.pop(key, None)
            self._blocked_until.pop(key, None)


@dataclass
class AppContext:
    auth_mode: str
    auth: AuthStore
    runtimes: WorkspaceRuntimeManager
    job_queue: DevelopmentJobQueue | None
    cookie_secure: bool
    allow_registration: bool
    login_limiter: LoginRateLimiter
    provider_limiter: ProviderCallRateLimiter

    @classmethod
    def from_env(cls, server_host: str) -> "AppContext":
        mode = _env("TANKAI_AUTH_MODE", "session").lower()
        if mode not in {"session", "disabled"}:
            raise RuntimeError("TANKAI_AUTH_MODE muss session oder disabled sein")
        is_loopback = server_host in {"127.0.0.1", "localhost", "::1"}
        if mode == "disabled" and not is_loopback:
            raise RuntimeError("Deaktivierte Authentifizierung ist nur auf Loopback erlaubt")
        data_root = Path(_env("TANKAI_DATA_ROOT", ".tankai/data")).resolve()
        auth_db = Path(_env("TANKAI_AUTH_DB", str(data_root / "auth.db"))).resolve()
        cookie_secure = _env_bool("TANKAI_COOKIE_SECURE", not is_loopback)
        auth = AuthStore(auth_db, session_hours=_safe_int("TANKAI_SESSION_HOURS", 12, 1, 720))
        job_queue = None
        if _env_bool("TANKAI_DEV_QUEUE_ENABLED", False):
            if mode != "session":
                raise RuntimeError("Die Development-Queue benötigt serverseitige Session-Authentifizierung")
            required = {
                "TANKAI_REPOSITORY_BASE": _env("TANKAI_REPOSITORY_BASE"),
                "TANKAI_WORKTREE_BASE": _env("TANKAI_WORKTREE_BASE"),
                "TANKAI_STATE_BASE": _env("TANKAI_STATE_BASE"),
            }
            missing = [key for key, value in required.items() if not value]
            if missing:
                raise RuntimeError("Development-Queue benötigt: " + ", ".join(missing))
            job_queue = DevelopmentJobQueue(
                _env("TANKAI_DEV_QUEUE_DB", str(data_root / "development-jobs.db")),
                auth_store=auth,
                repository_base=required["TANKAI_REPOSITORY_BASE"],
                workspace_base=required["TANKAI_WORKTREE_BASE"],
                state_base=required["TANKAI_STATE_BASE"],
            )
        return cls(
            auth_mode=mode,
            auth=auth,
            runtimes=WorkspaceRuntimeManager(data_root),
            job_queue=job_queue,
            cookie_secure=cookie_secure,
            allow_registration=_env_bool("TANKAI_ALLOW_REGISTRATION", False),
            login_limiter=LoginRateLimiter(
                limit=_safe_int("TANKAI_LOGIN_ATTEMPTS", 5, 3, 20),
                window_seconds=_safe_int("TANKAI_LOGIN_WINDOW", 300, 60, 3600),
                block_seconds=_safe_int("TANKAI_LOGIN_BLOCK", 900, 60, 86400),
            ),
            provider_limiter=ProviderCallRateLimiter(
                auth,
                limit=_safe_int("TANKAI_PROVIDER_CALLS_PER_WINDOW", 40, 1, 240),
                window_seconds=_safe_int(
                    "TANKAI_PROVIDER_RATE_WINDOW_SECONDS", 60, 1, 3600
                ),
            ),
        )

    def close(self) -> None:
        self.runtimes.close()


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="de">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>TankAI</title>
  <meta name="theme-color" content="#071820"/>
  <link rel="icon" href="/favicon.ico" sizes="any"/>
  <link rel="icon" type="image/png" sizes="256x256" href="/favicon.png"/>
  <link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png"/>
  <style>
    :root { --bg:#0b0f14; --card:#1a2332; --border:#243044; --accent:#3b82f6;
      --accent2:#8b5cf6; --text:#e8eef7; --muted:#8b9bb4; }
    * { box-sizing:border-box; }
    body { margin:0; font-family:system-ui,sans-serif; background:var(--bg); color:var(--text); }
    header { padding:1rem 1.5rem; border-bottom:1px solid var(--border); display:flex;
      justify-content:space-between; align-items:center; background:linear-gradient(90deg,#0f172a,#1e1b4b); }
    header h1 { margin:0; font-size:1.15rem; }
    .brand { display:flex; align-items:center; gap:.75rem; }
    .brand-mark { width:42px; height:42px; object-fit:contain; filter:drop-shadow(0 0 8px rgba(34,211,238,.4)); }
    .ver { color:var(--muted); font-size:.8rem; white-space:pre-wrap; }
    .layout { display:grid; grid-template-columns:1fr 300px; gap:1rem; max-width:1100px; margin:0 auto; padding:1rem; }
    .single { max-width:480px; margin:3rem auto; padding:1rem; }
    @media (max-width:900px){ .layout{grid-template-columns:1fr;} }
    .card { background:var(--card); border:1px solid var(--border); border-radius:12px; padding:1.1rem; margin-bottom:1rem; }
    .card h2 { margin:0 0 .7rem; font-size:.85rem; color:var(--muted); text-transform:uppercase; letter-spacing:.04em; }
    label { display:block; font-size:.8rem; color:var(--muted); margin-bottom:.3rem; }
    textarea,input,select { width:100%; background:var(--bg); border:1px solid var(--border); border-radius:8px;
      color:var(--text); padding:.65rem .75rem; margin-bottom:.7rem; font-size:.92rem; }
    textarea { min-height:100px; resize:vertical; }
    button { background:linear-gradient(135deg,var(--accent),var(--accent2)); color:#fff; border:none;
      border-radius:8px; padding:.7rem 1.3rem; font-weight:600; cursor:pointer; }
    button.secondary { background:#263348; }
    button:disabled { opacity:.45; cursor:not-allowed; }
    .badge { display:inline-block; padding:.15rem .5rem; border-radius:999px; font-size:.72rem;
      font-weight:600; margin:0 .25rem .25rem 0; }
    .b-ok { background:#14532d; color:#86efac; } .b-err { background:#7f1d1d; color:#fca5a5; }
    .b-info { background:#1e3a5f; color:#93c5fd; } .b-warn { background:#713f12; color:#fde68a; }
    #answer { white-space:pre-wrap; line-height:1.55; }
    table { width:100%; border-collapse:collapse; font-size:.85rem; }
    th,td { text-align:left; padding:.4rem .5rem; border-bottom:1px solid var(--border); vertical-align:top; }
    th { color:var(--muted); font-weight:500; }
    .hist-item { padding:.5rem 0; border-bottom:1px solid var(--border); cursor:pointer; font-size:.85rem; }
    .hist-item:hover,.hist-item:focus { color:var(--accent); outline:none; }
    #status,#loginStatus { color:var(--muted); font-size:.85rem; margin-left:.5rem; }
    .row { display:flex; gap:.5rem; align-items:center; } .row > * { flex:1; }
  </style>
</head>
<body>
  <header><div class="brand"><img class="brand-mark" src="/favicon.png" width="42" height="42" alt="" aria-hidden="true"/><div><h1>TankAI</h1><div class="ver">Mandantenfähiges Web Intelligence OS</div></div></div><div class="ver" id="health">…</div></header>
  <main id="loginView" class="single">
    <div class="card"><h2>Anmeldung</h2>
      <label for="email">E-Mail</label><input id="email" type="email" autocomplete="username" maxlength="254"/>
      <label for="password">Passwort</label><input id="password" type="password" autocomplete="current-password" maxlength="256"/>
      <button id="loginBtn" type="button">Anmelden</button><span id="loginStatus"></span>
    </div>
  </main>
  <main id="appView" class="layout" hidden>
    <div>
      <div class="card"><div class="row"><div><label for="workspace">Workspace</label><select id="workspace"></select></div>
        <div style="flex:0 0 auto;padding-top:1.1rem"><button id="logoutBtn" class="secondary" type="button">Abmelden</button></div></div></div>
      <div class="card">
        <h2>Neues Ziel</h2>
        <label for="goal">Ziel</label><textarea id="goal" maxlength="20000" placeholder="Was soll erreicht werden?"></textarea>
        <label for="dod">Definition of Done</label><input id="dod" maxlength="5000" value="Eine klare, überprüfbare Antwort liegt vor."/>
        <div style="margin-bottom:.7rem"><label style="display:inline"><input type="checkbox" id="parallel"/> Parallel</label></div>
        <button id="runBtn" type="button">TankAI starten</button><span id="status"></span>
      </div>
      <div class="card" id="outCard" hidden><div id="badges"></div><h2 style="margin-top:.75rem">Antwort</h2><div id="answer"></div></div>
      <div class="card" id="planCard" hidden><h2>Plan</h2><div id="rationale" class="ver" style="margin-bottom:.5rem"></div>
        <table><thead><tr><th>#</th><th>Typ</th><th>Beschreibung</th><th>Status</th></tr></thead><tbody id="planBody"></tbody></table></div>
      <div class="card" id="receiptCard" hidden><h2>Receipts</h2>
        <table><thead><tr><th>Actor</th><th>Action</th><th>OK</th><th>Summary</th></tr></thead><tbody id="receiptBody"></tbody></table></div>
    </div>
    <div><div class="card"><h2>System</h2><div id="sysInfo" class="ver">—</div></div>
      <div class="card"><h2>Verlauf</h2><div id="history" class="ver">Noch keine Runs.</div></div></div>
  </main>
<script nonce="__CSP_NONCE__">
'use strict';
const byId=(id)=>document.getElementById(id); let csrf=''; let currentUser=null;
function textCell(value){ const td=document.createElement('td'); td.textContent=String(value??''); return td; }
function badge(value,cls){ const span=document.createElement('span'); span.className='badge '+cls; span.textContent=String(value??''); return span; }
function clear(node){ node.replaceChildren(); }
async function jsonFetch(url,options={}){
  options.headers=Object.assign({},options.headers||{}); if(csrf && options.method && options.method!=='GET') options.headers['X-CSRF-Token']=csrf;
  const response=await fetch(url,options); let data;
  try{ data=await response.json(); }catch{ throw new Error('Ungültige Serverantwort'); }
  if(!response.ok || data.error){ const err=new Error(data.error||('HTTP '+response.status)); err.status=response.status; throw err; }
  return data;
}
function showLogin(){ byId('loginView').hidden=false; byId('appView').hidden=true; csrf=''; currentUser=null; }
function showApp(data){ currentUser=data; csrf=String(data.csrf_token||''); byId('loginView').hidden=true; byId('appView').hidden=false;
  const select=byId('workspace'); clear(select); (data.workspaces||[]).forEach((w)=>{ const o=document.createElement('option'); o.value=w.id; o.textContent=w.name+' ('+w.role+')'; o.selected=w.id===data.workspace.id; select.appendChild(o); });
}
async function refreshHealth(){ try{ const d=await jsonFetch('/api/health'); byId('health').textContent=(d.ok?'Online':'Offline')+' · '+d.version; }catch{ byId('health').textContent='Offline'; } }
async function loadMe(){ try{ const d=await jsonFetch('/api/auth/me'); showApp(d); await refreshSystem(); await loadHistory(); }catch(error){ if(error.status===401) showLogin(); else byId('loginStatus').textContent=String(error.message||error); } }
async function login(){ const email=byId('email').value.trim(); const password=byId('password').value; const btn=byId('loginBtn'); btn.disabled=true; byId('loginStatus').textContent='Prüfe …';
  try{ const d=await jsonFetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email,password})}); byId('password').value=''; showApp(d); await refreshSystem(); await loadHistory(); byId('loginStatus').textContent=''; }
  catch(error){ byId('loginStatus').textContent='Fehler: '+String(error.message||error); } finally{ btn.disabled=false; } }
async function logout(){ try{ await jsonFetch('/api/auth/logout',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}); }finally{ showLogin(); } }
async function selectWorkspace(){ try{ const d=await jsonFetch('/api/workspaces/select',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({workspace_id:byId('workspace').value})}); showApp(d); await refreshSystem(); await loadHistory(); }catch(error){ byId('status').textContent='Fehler: '+String(error.message||error); await loadMe(); } }
async function refreshSystem(){ try{ const d=await jsonFetch('/api/health'); byId('sysInfo').textContent='Nutzer: '+(d.user||'')+'\nWorkspace: '+(d.workspace||'')+'\nMain: '+(d.llm||'')+'\nCritic: '+(d.critic_llm||'')+'\nUnabhängig: '+String(Boolean(d.critic_independent))+'\nWeb: '+(d.web_research||'disabled')+'\nModus: '+(d.execution_mode||'unbekannt')+'\n'+(d.ltm||''); }catch(error){ byId('sysInfo').textContent='Systemdaten nicht verfügbar: '+String(error.message||error); } }
async function loadHistory(){ try{ const d=await jsonFetch('/api/history'); const el=byId('history'); clear(el); if(!d.length){ el.textContent='Noch keine Runs.'; return; } window.__hist=d; d.forEach((item,index)=>{ const row=document.createElement('div'); row.className='hist-item'; row.tabIndex=0; row.textContent=String(item.goal||'').slice(0,60); const show=()=>renderResult(window.__hist[index]); row.addEventListener('click',show); row.addEventListener('keydown',(event)=>{ if(event.key==='Enter'||event.key===' '){ event.preventDefault(); show(); } }); el.appendChild(row); }); }catch(error){ const el=byId('history'); clear(el); el.textContent='Verlauf nicht verfügbar: '+String(error.message||error); } }
function renderResult(data){ byId('outCard').hidden=false; byId('answer').textContent=String(data.final_answer||''); const badges=byId('badges'); clear(badges); const status=String(data.status||'unknown'); badges.appendChild(badge(status,status==='completed'?'b-ok':status==='simulated'?'b-warn':'b-err')); badges.appendChild(badge(String(data.duration_seconds??0)+'s','b-info')); badges.appendChild(badge(String(data.receipts_count??0)+' Receipts','b-info')); badges.appendChild(badge(String(data.execution_mode||'unknown'),'b-info')); badges.appendChild(badge(data.release_ready?'freigegeben':'nicht freigegeben',data.release_ready?'b-ok':'b-warn'));
  const planCard=byId('planCard'),planBody=byId('planBody'); clear(planBody); if(data.plan&&Array.isArray(data.plan.steps)){ planCard.hidden=false; byId('rationale').textContent=String(data.plan.rationale||''); data.plan.steps.forEach((step,index)=>{ const tr=document.createElement('tr'); tr.append(textCell(index+1),textCell(step.specialist_type),textCell(step.description),textCell(step.status||'—')); planBody.appendChild(tr); }); }else planCard.hidden=true;
  const receiptCard=byId('receiptCard'),receiptBody=byId('receiptBody'); clear(receiptBody); if(Array.isArray(data.receipts)&&data.receipts.length){ receiptCard.hidden=false; data.receipts.forEach((receipt)=>{ const tr=document.createElement('tr'); tr.append(textCell(receipt.actor),textCell(receipt.action),textCell(receipt.success?'✓':'✗'),textCell(String(receipt.output_summary||'').slice(0,80))); receiptBody.appendChild(tr); }); }else receiptCard.hidden=true; }
async function runTank(){ const goal=byId('goal').value.trim(),dod=byId('dod').value.trim(); if(!goal){ byId('status').textContent='Bitte ein Ziel eingeben.'; return; } const btn=byId('runBtn'); btn.disabled=true; byId('status').textContent='PLAN → ROUTE → VERIFY → LEARN …'; ['outCard','planCard','receiptCard'].forEach((id)=>byId(id).hidden=true); try{ const data=await jsonFetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({goal,definition_of_done:dod,parallel:byId('parallel').checked})}); renderResult(data); byId('status').textContent=data.status==='simulated'?'Simulation beendet — nicht verifiziert.':'Fertig.'; await loadHistory(); }catch(error){ if(error.status===401) showLogin(); else byId('status').textContent='Fehler: '+String(error.message||error); }finally{ btn.disabled=false; } }
byId('loginBtn').addEventListener('click',login); byId('password').addEventListener('keydown',(e)=>{if(e.key==='Enter')login();}); byId('logoutBtn').addEventListener('click',logout); byId('workspace').addEventListener('change',selectWorkspace); byId('runBtn').addEventListener('click',runTank);
refreshHealth(); loadMe(); setInterval(refreshHealth,15000);
</script>
</body>
</html>"""
HTML = HTML_TEMPLATE.replace("__CSP_NONCE__", _CSP_NONCE)


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

    def __init__(self, server_address, RequestHandlerClass, app: AppContext | None = None):
        super().__init__(server_address, RequestHandlerClass)
        self.app = app or AppContext.from_env(str(server_address[0]))

    def server_close(self) -> None:
        try:
            self.app.close()
        finally:
            super().server_close()


class Handler(BaseHTTPRequestHandler):
    server_version = "TankAI"
    sys_version = ""

    @property
    def app(self) -> AppContext:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, fmt, *args):
        print(f"[web] {self.address_string()} {fmt % args}")

    def _security_headers(self, *, cache_control: str = "no-store") -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", cache_control)
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; base-uri 'none'; object-src 'none'; "
            f"script-src 'nonce-{_CSP_NONCE}'; style-src 'self' 'unsafe-inline'; "
            "connect-src 'self'; frame-ancestors 'none'; form-action 'self'",
        )

    def _json(
        self,
        obj: object,
        code: int = 200,
        *,
        set_cookie: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self._security_headers()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _internal_error(self, operation: str) -> None:
        request_id = secrets.token_hex(6)
        print(f"[web] interner Fehler operation={operation} request_id={request_id}")
        traceback.print_exc()
        self._json({"error": f"Interner Serverfehler. Referenz: {request_id}"}, 500)

    def _read_json(self, *, max_bytes: int = 200_000) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._json({"error": "Ungültige Content-Length"}, 400)
            return None
        if length <= 0:
            self._json({"error": "Leerer Request"}, 400)
            return None
        if length > max_bytes:
            self._json({"error": "Payload zu groß"}, 413)
            return None
        try:
            raw = self.rfile.read(length)
            value = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._json({"error": "Ungültiges JSON"}, 400)
            return None
        if not isinstance(value, dict):
            self._json({"error": "JSON-Objekt erwartet"}, 400)
            return None
        return value

    def _session_token(self) -> str:
        header = self.headers.get("Cookie", "")
        jar = cookies.SimpleCookie()
        try:
            jar.load(header)
        except cookies.CookieError:
            return ""
        morsel = jar.get(_SESSION_COOKIE)
        return morsel.value if morsel else ""

    def _local_context(self) -> AuthContext:
        from datetime import datetime, timedelta, timezone
        return AuthContext(
            session_id="local-development",
            user_id="local-development",
            email="local@localhost",
            display_name="Local Development",
            tenant_id=_LOCAL_TENANT_ID,
            workspace_id=_LOCAL_WORKSPACE_ID,
            workspace_name="Lokal",
            role="owner",
            csrf_token="local-csrf",
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        )

    def _auth_context(self, *, required: bool = True) -> AuthContext | None:
        if self.app.auth_mode == "disabled":
            return self._local_context()
        context = self.app.auth.resolve_session(self._session_token())
        if context is None and required:
            self._json({"error": "Nicht angemeldet"}, 401)
        return context

    def _require_csrf(self, context: AuthContext) -> bool:
        if self.app.auth_mode == "disabled":
            return True
        supplied = self.headers.get("X-CSRF-Token", "")
        if self.app.auth.verify_csrf(context, supplied):
            return True
        self._json({"error": "Ungültiger CSRF-Token"}, 403)
        return False

    def _session_cookie(self, token: str, max_age: int) -> str:
        parts = [f"{_SESSION_COOKIE}={token}", "Path=/", "HttpOnly", "SameSite=Strict", f"Max-Age={max_age}"]
        if self.app.cookie_secure:
            parts.append("Secure")
        return "; ".join(parts)

    def _me_payload(self, context: AuthContext) -> dict[str, Any]:
        if self.app.auth_mode == "disabled":
            workspaces = [{"id": context.workspace_id, "tenant_id": context.tenant_id, "name": context.workspace_name, "slug": "local", "role": context.role}]
        else:
            workspaces = [vars(item) for item in self.app.auth.list_workspaces(context.user_id)]
        return {
            "user": {"id": context.user_id, "email": context.email, "display_name": context.display_name},
            "tenant": {"id": context.tenant_id},
            "workspace": {"id": context.workspace_id, "name": context.workspace_name, "role": context.role},
            "workspaces": workspaces,
            "csrf_token": context.csrf_token,
            "session_expires_at": context.expires_at.isoformat(),
        }

    @staticmethod
    def _development_job_payload(job) -> dict[str, Any]:
        return {
            "job_id": job.job_id,
            "repository_id": job.repository_id,
            "state": job.state.value,
            "priority": job.priority,
            "image": job.image,
            "memory_mb": job.memory_mb,
            "cpus": job.cpus,
            "pids_limit": job.pids_limit,
            "runtime_seconds": job.runtime_seconds,
            "attempts": job.attempts,
            "max_attempts": job.max_attempts,
            "created_at": job.created_at.isoformat(),
            "available_at": job.available_at.isoformat(),
            "started_at": job.started_at.isoformat() if job.started_at else None,
            "finished_at": job.finished_at.isoformat() if job.finished_at else None,
            "error": job.error,
        }

    @classmethod
    def _external_job_payload(cls, job) -> dict[str, Any]:
        payload = cls._development_job_payload(job)
        payload["error"] = "Development job failed" if job.error else ""
        payload["error_details_available"] = bool(job.error)
        receipt: dict[str, Any] | None = None
        if isinstance(job.result, dict) and isinstance(job.result.get("run"), dict):
            run = job.result["run"]
            receipt = {
                key: run[key]
                for key in (
                    "run_id",
                    "task_id",
                    "state",
                    "phase",
                    "branch",
                    "base_commit",
                    "execution_backend",
                    "changed_files",
                    "implementation_commit",
                    "rebased_from_commit",
                    "rebased_commit",
                    "integration_commit",
                    "started_at",
                    "finished_at",
                )
                if key in run
            }
        payload["result_available"] = job.result is not None
        payload["result_receipt"] = receipt
        return payload

    @staticmethod
    def _namespace_agent_pipeline(
        context: AgentAuthContext,
        idempotency_key: str,
        pipeline: WorkerPipelineJob,
    ) -> WorkerPipelineJob:
        """Replace untrusted agent identities with a stable per-job namespace."""
        key_digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:12]
        namespace = f"EXT_{context.agent_id.replace('-', '')[:12]}_{key_digest}"
        payload = pipeline.model_dump(mode="python")
        payload["worker"]["agent_id"] = f"{namespace}_WORKER"
        payload["gates"]["reviewer_agent_id"] = f"{namespace}_REVIEWER"
        payload["gates"]["qa_agent_id"] = f"{namespace}_QA"
        if payload["gates"].get("security_agent_id") is not None:
            payload["gates"]["security_agent_id"] = f"{namespace}_SECURITY"
        return WorkerPipelineJob.model_validate(payload)

    @staticmethod
    def _service_agent_payload(agent) -> dict[str, Any]:
        return {
            "agent_id": agent.agent_id,
            "name": agent.name,
            "description": agent.description,
            "workspace_id": agent.workspace_id,
            "owner_user_id": agent.owner_user_id,
            "active": agent.is_active,
            "created_at": agent.created_at.isoformat(),
            "updated_at": agent.updated_at.isoformat(),
        }

    @staticmethod
    def _agent_token_info_payload(token) -> dict[str, Any]:
        return {
            "token_id": token.token_id,
            "token_prefix": token.token_prefix,
            "label": token.label,
            "scopes": list(token.scopes),
            "repository_ids": list(token.repository_ids),
            "created_at": token.created_at.isoformat(),
            "expires_at": token.expires_at.isoformat(),
            "last_used_at": token.last_used_at.isoformat() if token.last_used_at else None,
            "revoked_at": token.revoked_at.isoformat() if token.revoked_at else None,
        }

    def _agent_context(self, *, scope: str | None = None) -> AgentAuthContext | None:
        authorization = self.headers.get("Authorization", "")
        scheme, separator, token = authorization.partition(" ")
        if (
            not separator
            or scheme.casefold() != "bearer"
            or not token
            or token.strip() != token
        ):
            self._json(
                {"error": "Gültiger Bearer-Token erforderlich"},
                401,
                headers={"WWW-Authenticate": 'Bearer realm="TankAICore External Agent API"'},
            )
            return None
        context = self.app.auth.resolve_agent_token(token)
        if context is None:
            self._json(
                {"error": "Agenten-Token ist ungültig, abgelaufen oder widerrufen"},
                401,
                headers={"WWW-Authenticate": 'Bearer realm="TankAICore External Agent API"'},
            )
            return None
        if scope is not None and not context.has_scope(scope):
            self._json({"error": f"Agenten-Scope fehlt: {scope}"}, 403)
            return None
        return context

    def _audit_agent(
        self,
        context: AgentAuthContext,
        event_type: str,
        *,
        success: bool,
        details: dict[str, Any] | None = None,
    ) -> None:
        payload = {"agent_id": context.agent_id, "token_id": context.token_id}
        payload.update(details or {})
        self.app.auth.audit(
            event_type,
            user_id=context.owner_user_id,
            workspace_id=context.workspace_id,
            success=success,
            details=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        )

    def _agent_job(self, context: AgentAuthContext, job_id: str):
        if not self.app.auth.agent_can_access_job(
            agent_id=context.agent_id, job_id=job_id
        ):
            raise PermissionError("Auftrag nicht gefunden oder nicht für diesen Agenten freigegeben")
        queue = self.app.job_queue
        if queue is None:
            raise QueueError("Development-Queue ist deaktiviert")
        return queue.get_job(
            actor_user_id=context.owner_user_id,
            workspace_id=context.workspace_id,
            job_id=job_id,
        )

    def _require_job_queue(self) -> DevelopmentJobQueue | None:
        if self.app.job_queue is None:
            self._json({"error": "Development-Queue ist deaktiviert"}, 404)
            return None
        return self.app.job_queue

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        brand_asset = _BRAND_ASSETS.get(path)
        if brand_asset is not None:
            content_type, body = brand_asset
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self._security_headers(cache_control="public, max-age=86400")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path in ("/", "/index.html"):
            body = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self._security_headers()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path.startswith("/api/v1/"):
            self._do_agent_get(path)
            return
        if path == "/api/health":
            context = self._auth_context(required=False)
            payload: dict[str, Any] = {
                "ok": True,
                "version": __version__,
                "auth_mode": self.app.auth_mode,
                "auth_required": self.app.auth_mode != "disabled",
                "registration_enabled": self.app.allow_registration,
                "production_ready": False,
                "development_queue_enabled": self.app.job_queue is not None,
            }
            if context is not None:
                try:
                    runtime = self.app.runtimes.get(tenant_id=context.tenant_id, workspace_id=context.workspace_id)
                    with runtime.lock:
                        tank = runtime.tank
                        main_sim = bool(tank.llm.is_simulation)
                        critic_sim = bool(tank.critic_llm.is_simulation)
                        mode = "simulation" if main_sim and critic_sim else "mixed" if (main_sim or critic_sim) else "live"
                        web_status = tank.tools.web_research_status()
                        payload.update({
                            "user": context.email,
                            "workspace": context.workspace_name,
                            "llm": tank.main_llm_identity,
                            "critic_llm": tank.critic_llm_identity,
                            "critic_independent": tank.critic_independent,
                            "web_research": web_status,
                            "execution_mode": mode,
                            "live_provider": mode == "live",
                            "verification_ready": mode == "live" and tank.critic_independent and web_status != "disabled",
                            "tenant_isolation": True,
                            "ltm": tank.ltm.summary() if tank.ltm else "",
                        })
                except Exception:
                    self._internal_error("health")
                    return
            self._json(payload)
            return
        if path == "/api/auth/me":
            context = self._auth_context()
            if context:
                self._json(self._me_payload(context))
            return
        if path == "/api/workspaces":
            context = self._auth_context()
            if not context:
                return
            self._json({"workspaces": self._me_payload(context)["workspaces"]})
            return
        if path == "/api/agents":
            context = self._auth_context()
            if not context:
                return
            if self.app.auth_mode == "disabled":
                self._json({"error": "KI-Agenten benötigen serverseitige Authentifizierung"}, 409)
                return
            try:
                agents = self.app.auth.list_service_agents(actor=context)
                self._json(
                    {"agents": [self._service_agent_payload(agent) for agent in agents]}
                )
            except PermissionError as exc:
                self._json({"error": str(exc)}, 403)
            return
        match = re.fullmatch(r"/api/agents/([0-9a-fA-F-]{36})/tokens", path)
        if match:
            context = self._auth_context()
            if not context:
                return
            try:
                UUID(match.group(1))
                tokens = self.app.auth.list_agent_tokens(
                    actor=context, agent_id=match.group(1)
                )
                self._json(
                    {"tokens": [self._agent_token_info_payload(token) for token in tokens]}
                )
            except PermissionError as exc:
                self._json({"error": str(exc)}, 403)
            except ValueError as exc:
                self._json({"error": str(exc)}, 404)
            return
        if path == "/api/dev/repositories":
            context = self._auth_context()
            if not context:
                return
            queue = self._require_job_queue()
            if queue is None:
                return
            try:
                repositories = queue.list_repositories(
                    actor_user_id=context.user_id, workspace_id=context.workspace_id
                )
                self._json({"repositories": [
                    {"repository_id": item.repository_id, "name": item.name, "enabled": item.enabled}
                    for item in repositories
                ]})
            except (PermissionError, QueueError, ValueError) as exc:
                self._json({"error": str(exc)}, 403)
            return
        if path == "/api/dev/jobs":
            context = self._auth_context()
            if not context:
                return
            queue = self._require_job_queue()
            if queue is None:
                return
            try:
                jobs = queue.list_jobs(
                    actor_user_id=context.user_id, workspace_id=context.workspace_id, limit=100
                )
                self._json({"jobs": [self._development_job_payload(item) for item in jobs]})
            except (PermissionError, QueueError, ValueError) as exc:
                self._json({"error": str(exc)}, 403)
            return
        if path == "/api/history":
            context = self._auth_context()
            if not context:
                return
            runtime = self.app.runtimes.get(tenant_id=context.tenant_id, workspace_id=context.workspace_id)
            self._json(runtime.history.list_recent(20))
            return
        self._json({"error": "Nicht gefunden"}, 404)

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path.startswith("/api/v1/"):
            self._do_agent_post(path)
            return
        if path == "/api/auth/login":
            self._login()
            return
        if path == "/api/auth/register":
            self._register()
            return

        context = self._auth_context()
        if not context:
            return
        if not self._require_csrf(context):
            return
        if path == "/api/auth/logout":
            if self.app.auth_mode != "disabled":
                self.app.auth.revoke_session(context.session_id, user_id=context.user_id)
            self._json({"ok": True}, set_cookie=self._session_cookie("", 0))
            return
        if path == "/api/workspaces/select":
            data = self._read_json(max_bytes=10_000)
            if data is None:
                return
            workspace_id = data.get("workspace_id")
            if not isinstance(workspace_id, str):
                self._json({"error": "workspace_id fehlt"}, 400)
                return
            try:
                UUID(workspace_id)
                if self.app.auth_mode == "disabled":
                    refreshed = context
                else:
                    refreshed = self.app.auth.select_workspace(context, workspace_id)
                self._json(self._me_payload(refreshed))
            except (ValueError, PermissionError) as exc:
                self._json({"error": str(exc)}, 403)
            return
        if path == "/api/workspaces":
            data = self._read_json(max_bytes=10_000)
            if data is None:
                return
            name = data.get("name")
            if not isinstance(name, str):
                self._json({"error": "name fehlt"}, 400)
                return
            if self.app.auth_mode == "disabled":
                self._json({"error": "Im lokalen Auth-Modus nicht verfügbar"}, 409)
                return
            try:
                workspace = self.app.auth.create_workspace(actor=context, name=name)
                self._json({"workspace": vars(workspace)}, 201)
            except PermissionError as exc:
                self._json({"error": str(exc)}, 403)
            except ValueError as exc:
                self._json({"error": str(exc)}, 400)
            return
        if path == "/api/agents":
            if self.app.auth_mode == "disabled":
                self._json({"error": "KI-Agenten benötigen serverseitige Authentifizierung"}, 409)
                return
            data = self._read_json(max_bytes=20_000)
            if data is None:
                return
            if set(data) - {"name", "description"}:
                self._json({"error": "Unbekannte Felder im Agentenauftrag"}, 400)
                return
            name = data.get("name")
            description = data.get("description", "")
            if not isinstance(name, str) or not isinstance(description, str):
                self._json({"error": "name und description müssen Text sein"}, 400)
                return
            try:
                agent = self.app.auth.create_service_agent(
                    actor=context, name=name, description=description
                )
                self._json({"agent": self._service_agent_payload(agent)}, 201)
            except PermissionError as exc:
                self._json({"error": str(exc)}, 403)
            except ValueError as exc:
                self._json({"error": str(exc)}, 400)
            return
        match = re.fullmatch(r"/api/agents/([0-9a-fA-F-]{36})/tokens", path)
        if match:
            queue = self._require_job_queue()
            if queue is None:
                return
            data = self._read_json(max_bytes=30_000)
            if data is None:
                return
            allowed_fields = {"scopes", "repository_ids", "expires_in_days", "label"}
            if set(data) - allowed_fields:
                self._json({"error": "Unbekannte Felder im Tokenauftrag"}, 400)
                return
            scopes = data.get("scopes")
            repository_ids = data.get("repository_ids")
            expires_in_days = data.get("expires_in_days", 30)
            label = data.get("label", "")
            if (
                not isinstance(scopes, list)
                or not isinstance(repository_ids, list)
                or not isinstance(label, str)
            ):
                self._json(
                    {"error": "scopes und repository_ids müssen Listen sein; label muss Text sein"},
                    400,
                )
                return
            try:
                UUID(match.group(1))
                visible = {
                    item.repository_id
                    for item in queue.list_repositories(
                        actor_user_id=context.user_id,
                        workspace_id=context.workspace_id,
                    )
                    if item.enabled
                }
                requested = {str(item).strip() for item in repository_ids}
                if not requested or not requested.issubset(visible):
                    raise PermissionError(
                        "Mindestens ein aktives Repository ist erforderlich und muss zum Workspace gehören"
                    )
                token = self.app.auth.create_agent_token(
                    actor=context,
                    agent_id=match.group(1),
                    scopes=scopes,
                    repository_ids=repository_ids,
                    expires_in_days=expires_in_days,
                    label=label,
                )
                self._json(
                    {
                        "token": {
                            "token_id": token.token_id,
                            "secret": token.token,
                            "token_prefix": token.token_prefix,
                            "scopes": list(token.scopes),
                            "repository_ids": list(token.repository_ids),
                            "created_at": token.created_at.isoformat(),
                            "expires_at": token.expires_at.isoformat(),
                            "shown_once": True,
                        }
                    },
                    201,
                )
            except PermissionError as exc:
                self._json({"error": str(exc)}, 403)
            except ValueError as exc:
                self._json({"error": str(exc)}, 400)
            return
        match = re.fullmatch(
            r"/api/agents/([0-9a-fA-F-]{36})/tokens/([0-9a-fA-F-]{36})/revoke",
            path,
        )
        if match:
            try:
                UUID(match.group(1))
                UUID(match.group(2))
                self.app.auth.revoke_agent_token(
                    actor=context, agent_id=match.group(1), token_id=match.group(2)
                )
                self._json({"ok": True})
            except PermissionError as exc:
                self._json({"error": str(exc)}, 403)
            except ValueError as exc:
                self._json({"error": str(exc)}, 404)
            return
        match = re.fullmatch(r"/api/agents/([0-9a-fA-F-]{36})/deactivate", path)
        if match:
            try:
                UUID(match.group(1))
                self.app.auth.deactivate_service_agent(
                    actor=context, agent_id=match.group(1)
                )
                self._json({"ok": True})
            except PermissionError as exc:
                self._json({"error": str(exc)}, 403)
            except ValueError as exc:
                self._json({"error": str(exc)}, 404)
            return
        if path == "/api/dev/jobs":
            queue = self._require_job_queue()
            if queue is None:
                return
            data = self._read_json(max_bytes=1_050_000)
            if data is None:
                return
            repository_id = data.get("repository_id")
            idempotency_key = data.get("idempotency_key")
            pipeline_raw = data.get("pipeline")
            priority = data.get("priority", 0)
            if not isinstance(repository_id, str) or not isinstance(idempotency_key, str) or not isinstance(pipeline_raw, dict):
                self._json({"error": "repository_id, idempotency_key und pipeline sind erforderlich"}, 400)
                return
            if not isinstance(priority, int) or isinstance(priority, bool):
                self._json({"error": "priority muss eine Ganzzahl sein"}, 400)
                return
            try:
                job = queue.enqueue(
                    actor_user_id=context.user_id,
                    workspace_id=context.workspace_id,
                    repository_id=repository_id,
                    pipeline=WorkerPipelineJob.model_validate(pipeline_raw),
                    idempotency_key=idempotency_key,
                    priority=priority,
                )
                self.app.auth.audit(
                    "development_job_enqueue",
                    user_id=context.user_id,
                    workspace_id=context.workspace_id,
                    success=True,
                    details=json.dumps({"job_id": job.job_id, "repository_id": repository_id}),
                )
                self._json({"job": self._development_job_payload(job)}, 202)
            except PermissionError as exc:
                self.app.auth.audit(
                    "development_job_enqueue", user_id=context.user_id,
                    workspace_id=context.workspace_id, success=False,
                    details='{"reason":"permission"}',
                )
                self._json({"error": str(exc)}, 403)
            except PydanticValidationError:
                self.app.auth.audit(
                    "development_job_enqueue", user_id=context.user_id,
                    workspace_id=context.workspace_id, success=False,
                    details='{"reason":"validation"}',
                )
                self._json({"error": "Ungültiger Worker-Auftrag"}, 400)
            except (QueueError, ValueError) as exc:
                self.app.auth.audit(
                    "development_job_enqueue", user_id=context.user_id,
                    workspace_id=context.workspace_id, success=False,
                    details='{"reason":"admission"}',
                )
                self._json({"error": str(exc)}, 400)
            return
        match = re.fullmatch(r"/api/dev/jobs/([0-9a-fA-F-]{36})/cancel", path)
        if match:
            queue = self._require_job_queue()
            if queue is None:
                return
            try:
                UUID(match.group(1))
                job = queue.cancel_job(
                    actor_user_id=context.user_id,
                    workspace_id=context.workspace_id,
                    job_id=match.group(1),
                )
                self._json({"job": self._development_job_payload(job)})
            except PermissionError as exc:
                self._json({"error": str(exc)}, 403)
            except (QueueError, ValueError) as exc:
                self._json({"error": str(exc)}, 409)
            return
        if path == "/api/run":
            self._run(context)
            return
        self._json({"error": "Nicht gefunden"}, 404)

    def _do_agent_get(self, path: str) -> None:
        if path == "/api/v1/capabilities":
            context = self._agent_context()
            if context is None:
                return
            policy_payload: dict[str, Any] | None = None
            if self.app.job_queue is not None:
                policy = self.app.job_queue.get_policy(context.workspace_id)
                if policy is not None:
                    policy_payload = {
                        "enabled": policy.enabled,
                        "max_queued": policy.max_queued,
                        "max_running": policy.max_running,
                        "max_memory_mb": policy.max_memory_mb,
                        "max_cpus": policy.max_cpus,
                        "max_pids": policy.max_pids,
                        "max_runtime_seconds": policy.max_runtime_seconds,
                        "max_attempts": policy.max_attempts,
                    }
            self._json(
                {
                    "api_version": "v1",
                    "agent": {
                        "agent_id": context.agent_id,
                        "name": context.agent_name,
                        "workspace_id": context.workspace_id,
                    },
                    "scopes": sorted(context.scopes),
                    "available_scopes": sorted(AGENT_TOKEN_SCOPES),
                    "repository_ids": sorted(context.repository_ids),
                    "token_expires_at": context.expires_at.isoformat(),
                    "development_queue_enabled": self.app.job_queue is not None,
                    "queue_policy": policy_payload,
                }
            )
            return
        if path == "/api/v1/repositories":
            context = self._agent_context(scope="repositories:read")
            if context is None:
                return
            queue = self._require_job_queue()
            if queue is None:
                return
            try:
                repositories = queue.list_repositories(
                    actor_user_id=context.owner_user_id,
                    workspace_id=context.workspace_id,
                )
                self._json(
                    {
                        "repositories": [
                            {
                                "repository_id": item.repository_id,
                                "name": item.name,
                                "enabled": item.enabled,
                            }
                            for item in repositories
                            if item.enabled and item.repository_id in context.repository_ids
                        ]
                    }
                )
            except (PermissionError, QueueError, ValueError) as exc:
                self._audit_agent(
                    context,
                    "agent_repository_list",
                    success=False,
                    details={"reason": "authorization"},
                )
                self._json({"error": str(exc)}, 403)
            return
        if path == "/api/v1/jobs":
            context = self._agent_context(scope="jobs:read")
            if context is None:
                return
            queue = self._require_job_queue()
            if queue is None:
                return
            jobs = []
            try:
                for job_id in self.app.auth.agent_job_ids(
                    agent_id=context.agent_id, limit=100
                ):
                    try:
                        job = queue.get_job(
                            actor_user_id=context.owner_user_id,
                            workspace_id=context.workspace_id,
                            job_id=job_id,
                        )
                    except (PermissionError, QueueError, ValueError):
                        continue
                    if job.repository_id in context.repository_ids:
                        jobs.append(self._external_job_payload(job))
                self._json({"jobs": jobs})
            except (PermissionError, QueueError, ValueError) as exc:
                self._json({"error": str(exc)}, 403)
            return
        match = re.fullmatch(r"/api/v1/jobs/([0-9a-fA-F-]{36})", path)
        if match:
            context = self._agent_context(scope="jobs:read")
            if context is None:
                return
            try:
                UUID(match.group(1))
                job = self._agent_job(context, match.group(1))
                if job.repository_id not in context.repository_ids:
                    raise PermissionError(
                        "Repository ist für diesen KI-Agenten nicht freigegeben"
                    )
                self._json({"job": self._external_job_payload(job)})
            except PermissionError as exc:
                self._json({"error": str(exc)}, 404)
            except (QueueError, ValueError) as exc:
                self._json({"error": str(exc)}, 409)
            return
        self._json({"error": "Nicht gefunden"}, 404)

    def _do_agent_post(self, path: str) -> None:
        if path == "/api/v1/jobs":
            context = self._agent_context(scope="jobs:submit")
            if context is None:
                return
            queue = self._require_job_queue()
            if queue is None:
                return
            data = self._read_json(max_bytes=1_050_000)
            if data is None:
                return
            if set(data) - {"repository_id", "idempotency_key", "pipeline", "priority"}:
                self._json({"error": "Unbekannte Felder im Entwicklungsauftrag"}, 400)
                return
            repository_id = data.get("repository_id")
            idempotency_key = data.get("idempotency_key")
            pipeline_raw = data.get("pipeline")
            priority = data.get("priority", 0)
            if (
                not isinstance(repository_id, str)
                or not isinstance(idempotency_key, str)
                or not isinstance(pipeline_raw, dict)
            ):
                self._json(
                    {"error": "repository_id, idempotency_key und pipeline sind erforderlich"},
                    400,
                )
                return
            if not isinstance(priority, int) or isinstance(priority, bool):
                self._json({"error": "priority muss eine Ganzzahl sein"}, 400)
                return
            clean_key = idempotency_key.strip()
            if (
                not clean_key
                or len(clean_key) > 150
                or any(ord(char) < 32 for char in clean_key)
            ):
                self._json({"error": "Ungültiger Idempotency-Key"}, 400)
                return
            if repository_id not in context.repository_ids:
                self._audit_agent(
                    context,
                    "agent_job_enqueue",
                    success=False,
                    details={"reason": "repository_scope"},
                )
                self._json(
                    {"error": "Repository ist für diesen KI-Agenten nicht freigegeben"}, 403
                )
                return
            try:
                parsed_pipeline = WorkerPipelineJob.model_validate(pipeline_raw)
                namespaced_pipeline = self._namespace_agent_pipeline(
                    context, clean_key, parsed_pipeline
                )
                job = queue.enqueue(
                    actor_user_id=context.owner_user_id,
                    workspace_id=context.workspace_id,
                    repository_id=repository_id,
                    pipeline=namespaced_pipeline,
                    idempotency_key=f"agent:{context.agent_id}:{clean_key}",
                    priority=priority,
                )
                self.app.auth.grant_agent_job(
                    context=context,
                    job_id=job.job_id,
                    repository_id=repository_id,
                )
                self._audit_agent(
                    context,
                    "agent_job_enqueue",
                    success=True,
                    details={"job_id": job.job_id, "repository_id": repository_id},
                )
                self._json({"job": self._external_job_payload(job)}, 202)
            except PermissionError as exc:
                self._audit_agent(
                    context,
                    "agent_job_enqueue",
                    success=False,
                    details={"reason": "permission"},
                )
                self._json({"error": str(exc)}, 403)
            except PydanticValidationError:
                self._audit_agent(
                    context,
                    "agent_job_enqueue",
                    success=False,
                    details={"reason": "validation"},
                )
                self._json({"error": "Ungültiger Worker-Auftrag"}, 400)
            except (QueueError, ValueError) as exc:
                self._audit_agent(
                    context,
                    "agent_job_enqueue",
                    success=False,
                    details={"reason": "admission"},
                )
                self._json({"error": str(exc)}, 400)
            return
        match = re.fullmatch(r"/api/v1/jobs/([0-9a-fA-F-]{36})/cancel", path)
        if match:
            context = self._agent_context(scope="jobs:cancel")
            if context is None:
                return
            queue = self._require_job_queue()
            if queue is None:
                return
            try:
                UUID(match.group(1))
                current = self._agent_job(context, match.group(1))
                if current.repository_id not in context.repository_ids:
                    raise PermissionError(
                        "Repository ist für diesen KI-Agenten nicht freigegeben"
                    )
                job = queue.cancel_job(
                    actor_user_id=context.owner_user_id,
                    workspace_id=context.workspace_id,
                    job_id=match.group(1),
                )
                self._audit_agent(
                    context,
                    "agent_job_cancel",
                    success=True,
                    details={"job_id": job.job_id},
                )
                self._json({"job": self._external_job_payload(job)})
            except PermissionError as exc:
                self._json({"error": str(exc)}, 404)
            except (QueueError, ValueError) as exc:
                self._json({"error": str(exc)}, 409)
            return
        self._json({"error": "Nicht gefunden"}, 404)

    def _login(self) -> None:
        if self.app.auth_mode == "disabled":
            self._json(self._me_payload(self._local_context()))
            return
        data = self._read_json(max_bytes=10_000)
        if data is None:
            return
        email = data.get("email")
        password = data.get("password")
        if not isinstance(email, str) or not isinstance(password, str):
            self._json({"error": "E-Mail und Passwort erforderlich"}, 400)
            return
        key = f"{self.client_address[0]}:{email.strip().casefold()}"
        if not self.app.login_limiter.allowed(key):
            self._json({"error": "Zu viele Anmeldeversuche. Später erneut versuchen."}, 429)
            return
        session = self.app.auth.authenticate(
            email=email,
            password=password,
            user_agent=self.headers.get("User-Agent", "")[:500],
            client_ip=self.client_address[0],
        )
        if session is None:
            self.app.login_limiter.failure(key)
            self._json({"error": "Anmeldung fehlgeschlagen"}, 401)
            return
        self.app.login_limiter.success(key)
        remaining = max(1, int((session.context.expires_at.timestamp() - time.time())))
        self._json(self._me_payload(session.context), set_cookie=self._session_cookie(session.token, remaining))

    def _register(self) -> None:
        if self.app.auth_mode == "disabled" or not self.app.allow_registration:
            self._json({"error": "Registrierung ist deaktiviert"}, 403)
            return
        data = self._read_json(max_bytes=20_000)
        if data is None:
            return
        values = {key: data.get(key) for key in ("email", "password", "display_name", "tenant_name", "workspace_name")}
        if not all(isinstance(values[key], str) for key in ("email", "password", "display_name", "tenant_name")):
            self._json({"error": "Pflichtfelder fehlen"}, 400)
            return
        try:
            self.app.auth.create_user_with_tenant(
                email=values["email"],
                password=values["password"],
                display_name=values["display_name"],
                tenant_name=values["tenant_name"],
                workspace_name=values["workspace_name"] if isinstance(values["workspace_name"], str) else "Standard",
            )
        except ValueError as exc:
            self._json({"error": str(exc)}, 400)
            return
        session = self.app.auth.authenticate(email=values["email"], password=values["password"], user_agent=self.headers.get("User-Agent", "")[:500], client_ip=self.client_address[0])
        if session is None:
            self._internal_error("register_login")
            return
        remaining = max(1, int((session.context.expires_at.timestamp() - time.time())))
        self._json(self._me_payload(session.context), 201, set_cookie=self._session_cookie(session.token, remaining))

    def _run(self, context: AuthContext) -> None:
        data = self._read_json()
        if data is None:
            return
        goal_raw = data.get("goal") or data.get("goal_description") or ""
        dod_raw = data.get("definition_of_done") or "Eine klare, überprüfbare Antwort liegt vor."
        if not isinstance(goal_raw, str) or not isinstance(dod_raw, str):
            self._json({"error": "goal und definition_of_done müssen Text sein"}, 400)
            return
        goal, dod = goal_raw.strip(), dod_raw.strip()
        if not goal:
            self._json({"error": "goal fehlt"}, 400)
            return
        if len(goal) > 20_000 or len(dod) > 5_000:
            self._json({"error": "Eingabe zu lang"}, 400)
            return
        parallel = data.get("parallel", False)
        if not isinstance(parallel, bool):
            self._json({"error": "parallel muss true oder false sein"}, 400)
            return
        try:
            runtime = self.app.runtimes.get(tenant_id=context.tenant_id, workspace_id=context.workspace_id)
            with runtime.lock:
                tank = runtime.tank
                tank.parallel = parallel
                tank.llm_call_budget.set_call_guard(
                    lambda identity: self.app.provider_limiter.consume(context.user_id, identity)
                )
                try:
                    result = tank.run(goal_description=goal, definition_of_done=dod)
                finally:
                    tank.llm_call_budget.set_call_guard(None)
                plan_data = None
                if result.plan:
                    plan_data = {
                        "rationale": result.plan.rationale,
                        "steps": [{
                            "specialist_type": step.specialist_type,
                            "description": step.description,
                            "status": step.status.value if hasattr(step.status, "value") else str(step.status),
                        } for step in result.plan.steps],
                    }
                receipts = [{
                    "actor": receipt.actor.value if hasattr(receipt.actor, "value") else str(receipt.actor),
                    "action": receipt.action,
                    "success": receipt.success,
                    "output_summary": receipt.output_summary,
                    "details": receipt.details,
                } for receipt in result.receipts]
                payload = {
                    "workspace_id": context.workspace_id,
                    "goal": goal,
                    "final_answer": result.final_answer,
                    "status": result.status.value if hasattr(result.status, "value") else str(result.status),
                    "execution_mode": result.execution_mode,
                    "duration_seconds": result.duration_seconds,
                    "receipts_count": len(result.receipts),
                    "receipts": receipts,
                    "plan": plan_data,
                    "main_llm": tank.main_llm_identity,
                    "critic_llm": tank.critic_llm_identity,
                    "critic_independent": tank.critic_independent,
                    "verification_passed": result.verification_passed,
                    "release_ready": result.release_ready,
                    "plan_gate_passed": result.plan_gate_passed,
                    "failed_step_ids": result.failed_step_ids,
                    "web_research": result.web_research_provider,
                    "source_ids": result.source_ids,
                    "source_urls": result.source_urls,
                    "ltm": tank.ltm.summary() if tank.ltm else "",
                }
                runtime.history.append(payload)
            self.app.auth.audit("run", user_id=context.user_id, workspace_id=context.workspace_id, success=True, details=json.dumps({"goal_id": result.goal_id, "status": payload["status"]}))
            self._json(payload)
        except LLMRateLimitExceeded as exc:
            self.app.auth.audit(
                "run",
                user_id=context.user_id,
                workspace_id=context.workspace_id,
                success=False,
                details=json.dumps({
                    "reason": "provider_rate_limit",
                    "provider": exc.provider,
                    "retry_after_seconds": exc.retry_after_seconds,
                }),
            )
            self._json(
                {
                    "error": "Provider-Rate-Limit erreicht. Später erneut versuchen.",
                    "provider": exc.provider,
                    "retry_after_seconds": exc.retry_after_seconds,
                },
                429,
                headers={"Retry-After": str(exc.retry_after_seconds)},
            )
        except Exception:
            self.app.auth.audit("run", user_id=context.user_id, workspace_id=context.workspace_id, success=False)
            self._internal_error("run")


def main() -> None:
    host = _env("TANKAI_HOST", "127.0.0.1") or "127.0.0.1"
    port = _safe_int("TANKAI_PORT", 8765, 1, 65535)
    app = AppContext.from_env(host)
    if app.auth_mode == "session" and app.auth.user_count() == 0:
        print("[web] WARNUNG: Keine Benutzer vorhanden. Zuerst auth_cli create-user ausführen.")
    server = ThreadedHTTPServer((host, port), Handler, app=app)
    print(f"TankAI Web-UI → http://{host}:{port}  (Auth: {app.auth_mode}, Registrierung: {'an' if app.allow_registration else 'aus'})")
    print("Ctrl+C zum Beenden")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
