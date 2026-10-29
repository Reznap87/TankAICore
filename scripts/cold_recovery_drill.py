"""Synthetic cold recovery rehearsal. Never accepts a production source or key.

Developer/operator test tooling only, not a production backup format or service.
The sole writer exits before checkpoint/capture; two fresh verifier processes
use a new root after the synthetic source has been removed.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import closing, contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
from http.cookiejar import CookieJar
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
from urllib.error import HTTPError
from urllib.request import build_opener, HTTPCookieProcessor, ProxyHandler, Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

MAGIC = b"TANKAI-SYNTHETIC-COLD-DRILL\0\1"
LIMIT = 16 * 1024 * 1024
NOW = 1_790_000_000
PASSWORD = "Synthetic-recovery-only-password-123"
EMAILS = ("owner@recovery.example.test", "other@recovery.example.test")


class DrillError(RuntimeError):
    pass


def need(condition, message):
    if not condition:
        raise DrillError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def network_guard():
    def audit(event, args):
        if event in {"socket.connect", "socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr"}:
            address = args[1] if event == "socket.connect" else args[0]
            host = address[0] if isinstance(address, tuple) else address
            try:
                allowed = ipaddress.ip_address(host).is_loopback
            except (ValueError, TypeError):
                allowed = False
            if not allowed:
                raise DrillError("non-loopback network access forbidden in recovery drill")
    sys.addaudithook(audit)


def configure(root):
    # Prevent inherited bindings, bootstrap owners, real SDK keys and dotenv reads.
    for key in list(os.environ):
        if key.startswith(("TANKAI_", "OPENAI_", "ANTHROPIC_")):
            del os.environ[key]
    os.environ.update(TANKAI_DATA_ROOT=str(root), TANKAI_AUTH_DB=str(root / "auth.db"),
        TANKAI_AUTH_MODE="session", TANKAI_COOKIE_SECURE="0", TANKAI_LLM="mock",
        TANKAI_CRITIC_LLM="mock", TANKAI_EMBEDDER="hashing",
        TANKAI_REQUIRE_RESEARCH_EVIDENCE="0", TANKAI_ALLOW_REGISTRATION="0",
        TANKAI_DEV_QUEUE_ENABLED="0")
    from tankai.core import llm
    llm._load_dotenv_if_available = lambda: None
    network_guard()


class Client:
    def __init__(self, base):
        self.base = base
        self.opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))

    def request(self, path, payload=None, csrf=None):
        request = Request(self.base + path,
            data=None if payload is None else canonical(payload),
            headers={"Content-Type":"application/json", **({"X-CSRF-Token":csrf} if csrf else {})})
        try:
            response = self.opener.open(request, timeout=10)
        except HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response)

    def login(self, email):
        status, body = self.request("/api/auth/login", {"email":email, "password":PASSWORD})
        need(status == 200, "restored login failed")
        return body


@contextmanager
def serving(app):
    from tankai.web.server import ThreadedHTTPServer, Handler
    server = ThreadedHTTPServer(("127.0.0.1", 0), Handler, app=app)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)
        app.close()
        need(not thread.is_alive(), "drill HTTP server did not stop")


def workspace_path(root, account):
    return root / "tenants" / account["tenant"] / "workspaces" / account["workspace"]


def seed(root, expected_file):
    from tankai.core.monetary_budget import MonetaryBudget, PilotPolicy, PriceQuote
    from tankai.web.server import AppContext
    root.mkdir()  # Existing sources, including links, can never be selected.
    configure(root)
    app = AppContext.from_env("127.0.0.1")
    metadata = {"accounts":[], "synthetic":True}
    with serving(app) as base:
        for index, email in enumerate(EMAILS):
            owner, tenant, workspace = app.auth.create_user_with_tenant(email=email,
                password=PASSWORD, display_name="Synthetic owner", tenant_name="Synthetic tenant")
            account = {"owner":owner, "tenant":tenant, "workspace":workspace,
                       "marker":f"recovery-workspace-{index}"}
            client = Client(base)
            session = client.login(email)
            status, response = client.request("/api/run", {"goal":"Formuliere " + account["marker"]},
                                               session["csrf_token"])
            need(status == 200 and response["execution_mode"] == "simulation", "synthetic chat seed failed")
            account["answer"] = response["final_answer"]
            with app.runtimes.acquire(tenant_id=tenant, workspace_id=workspace) as runtime:
                marker = account["marker"]
                account["memory"] = runtime.tank.memory.add(marker, "synthetic", validity="valid").id
                account["semantic"] = runtime.tank.ltm.add_semantic(marker + " vector", source="synthetic").id
                cold = runtime.tank.ltm.add_semantic(marker + " full cold archive content", source="synthetic")
                need(runtime.tank.ltm._move_to_cold(cold.id), "synthetic cold archive was not written")
                account["cold"] = cold.id
            metadata["accounts"].append(account)
        session = app.auth.authenticate(email=EMAILS[0], password=PASSWORD)
        metadata["old_session"] = session.token
        agent = app.auth.create_service_agent(actor=session.context, name="Synthetic recovery agent")
        token = app.auth.create_agent_token(actor=session.context, agent_id=agent.agent_id,
            scopes=["jobs:read"], repository_ids=["00000000-0000-4000-8000-000000000101"])
        metadata["old_agent_token"] = token.token
    owner = metadata["accounts"][0]
    policy = PilotPolicy(owner["owner"], NOW, NOW + 86_400)
    quote = PriceQuote("synthetic", "openai", "synthetic-model", NOW, NOW+86_400,
                        6000, "https://example.test/pricing", "Synthetic fixed all-in ceiling")
    with MonetaryBudget.create(root / "monetary.sqlite", policy, clock=lambda:NOW) as ledger:
        ledger.begin_job("completed", owner_id=owner["owner"])
        ledger.reserve("completed", "settled", owner_id=owner["owner"], quote=quote)
        ledger.settle("settled", owner_id=owner["owner"], actual_micro_eur=3500)
        ledger.close_job("completed", owner_id=owner["owner"])
        ledger.begin_job("interrupted", owner_id=owner["owner"])
        ledger.reserve("interrupted", "unknown", owner_id=owner["owner"], quote=quote)
        ledger.settle("unknown", owner_id=owner["owner"], actual_micro_eur=None)
        metadata["budget"] = ledger.snapshot()
    text = "Synthetic approved source: recovery value 42."
    grant = {"schema_version":1, "owner_id":owner["owner"], "tenant_id":owner["tenant"],
        "workspace_id":owner["workspace"], "valid_from":NOW, "valid_until":NOW+86_400,
        "approval_id":"synthetic-recovery-grant", "sources":[{"title":"Synthetic source",
        "url":"https://example.test/approved", "text":text, "sha256":digest(text.encode())}]}
    raw = canonical(grant)
    (root / "approved-source.json").write_bytes(raw)
    metadata["source_pin"] = digest(raw)
    metadata["quote"] = asdict(quote)
    expected_file.write_bytes(canonical(metadata))


def inventory(root):
    files = {}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        need(not path.is_symlink(), "links are forbidden in the synthetic cold image")
        if stat.S_ISDIR(info.st_mode):
            continue
        need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and 0 < info.st_size <= LIMIT,
             "invalid synthetic cold image file")
        need(not path.name.endswith(("-wal", "-shm", "-journal")), "uncheckpointed SQLite sidecar")
        files[path.relative_to(root).as_posix()] = path.read_bytes()
        need(len(files) <= 48 and sum(map(len, files.values())) <= LIMIT // 2, "cold image exceeds drill bounds")
    return files


def seal(files, key):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = secrets.token_bytes(12)
    payload = canonical({name:base64.b64encode(data).decode() for name, data in files.items()})
    need(len(payload) <= LIMIT, "encoded cold image exceeds drill bounds")
    return MAGIC + nonce + AESGCM(key).encrypt(nonce, payload, MAGIC)


def decode(data, key, expected_pin, manifest):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.exceptions import InvalidTag
    need(type(data) is bytes and len(MAGIC)+28 <= len(data) <= LIMIT+len(MAGIC)+28,
         "invalid drill envelope size")
    need(data.startswith(MAGIC) and digest(data) == expected_pin, "drill envelope pin mismatch")
    try:
        raw = AESGCM(key).decrypt(data[len(MAGIC):len(MAGIC)+12], data[len(MAGIC)+12:], MAGIC)
    except InvalidTag:
        raise DrillError("drill envelope authentication failed") from None
    records = json.loads(raw)
    need(type(records) is dict and set(records) == set(manifest) and 1 <= len(records) <= 48,
         "missing, mixed or extra cold image component")
    result = {}
    for name, encoded in records.items():
        path = PurePosixPath(name)
        need(type(name) is str and not path.is_absolute() and "\\" not in name and ":" not in name
             and all(part not in {".", ".."} for part in path.parts) and str(path) == name,
             "unsafe cold image path")
        value = base64.b64decode(encoded, validate=True)
        need(digest(value) == manifest[name], "cold image component hash mismatch")
        result[name] = value
    return result


def restore(data, key, expected_pin, manifest, destination):
    files = decode(data, key, expected_pin, manifest)  # All validation before writes.
    destination.mkdir()  # New root only; never replace an existing destination.
    for name, value in files.items():
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as output:
            output.write(value)
            output.flush()
            os.fsync(output.fileno())
    need(inventory(destination) == files, "restored cold image failed read-back verification")


def verify(root, expected_file):
    configure(root)
    from tankai.core.llm import OpenAILLM, AnthropicLLM
    from tankai.core.monetary_budget import MonetaryBudget, PilotPolicy, PriceQuote
    from tankai.core.request_pricing import TextTariff
    from tankai.web import runtime as runtime_module
    from tankai.web.auth import AuthStore
    from tankai.web.auth_state import _validate_snapshot
    from tankai.web.owner_pilot import OwnerPilot
    from tankai.web.server import AppContext
    from tankai.web.source_manifest import load_source_manifest
    metadata = json.loads(expected_file.read_bytes())
    need(metadata["synthetic"] is True, "only synthetic drill data may be opened")
    _validate_snapshot(str(root / "auth.db"))
    # Recovery requires fresh login: copied sessions/service tokens are revoked.
    with closing(sqlite3.connect(root / "auth.db")) as connection, connection:
        need(set(row[0] for row in connection.execute("SELECT email FROM users")) == set(EMAILS),
             "refusing non-synthetic account data")
        now = datetime.now(timezone.utc).isoformat()
        connection.execute("UPDATE sessions SET revoked_at=?", (now,))
        connection.execute("UPDATE agent_tokens SET revoked_at=?", (now,))
    auth = AuthStore(root / "auth.db")
    need(auth.resolve_session(metadata["old_session"]) is None, "copied session remained valid")
    need(auth.resolve_agent_token(metadata["old_agent_token"]) is None, "copied agent token remained valid")
    grant = load_source_manifest(root / "approved-source.json", expected_sha256=metadata["source_pin"])
    owner = metadata["accounts"][0]
    sends = []
    def forbidden(*args, **kwargs):
        sends.append(True)
        raise DrillError("recovery must not invoke a model")
    OpenAILLM.complete = AnthropicLLM.complete = forbidden
    endpoints = {("openai", "synthetic-main"):"https://example.test/v1",
                 ("anthropic", "synthetic-critic"):"https://example.test"}
    runtime_module.get_llm = lambda: OpenAILLM(model="synthetic-main", api_key="synthetic-key",
                                               base_url=endpoints[("openai", "synthetic-main")])
    os.environ["ANTHROPIC_BASE_URL"] = "https://example.test"
    runtime_module.get_critic_llm = lambda default: AnthropicLLM(model="synthetic-critic", api_key="synthetic-key")
    tariffs = {identity:TextTariff("synthetic", *identity, NOW, NOW+86_400,
        "https://example.test/pricing", "EUR", "1", "1", "0", "1", "0", "0", "0", None, True)
        for identity in endpoints}
    with MonetaryBudget(root / "monetary.sqlite", clock=lambda:NOW) as ledger:
        need(ledger.snapshot() == metadata["budget"], "budget state changed during cold recovery")
        need(ledger.policy == PilotPolicy(**metadata["budget"]["policy"]), "recovered pilot policy changed")
        replay = ledger.reserve("interrupted", "unknown", owner_id=owner["owner"],
                                quote=PriceQuote(**metadata["quote"]))
        need(not replay.dispatch_allowed, "restored attempt replay was authorized")
        pilot = OwnerPilot(ledger, tenant_id=owner["tenant"], workspace_ids=frozenset([owner["workspace"]]),
            tariffs=tariffs, endpoints=endpoints, input_token_bound=lambda request:1000,
            clock=lambda:NOW, source_manifests=(grant,))
        app = AppContext.from_env("127.0.0.1", owner_pilot=pilot)
        with serving(app) as base:
            for index, account in enumerate(metadata["accounts"]):
                client = Client(base)
                session = client.login(EMAILS[index])
                need(session["user"]["id"] == account["owner"] and session["workspace"]["role"] == "owner",
                     "restored account or role mismatch")
                status, history = client.request("/api/history")
                need(status == 200 and len(history) == 1 and history[0]["final_answer"] == account["answer"]
                     and history[0]["goal"] == "Formuliere " + account["marker"], "chat history mismatch")
                foreign = metadata["accounts"][1-index]["workspace"]
                status, _ = client.request("/api/workspaces/select", {"workspace_id":foreign}, session["csrf_token"])
                need(status == 403, "cross-tenant workspace access was allowed")
                status, _ = client.request("/api/run", {"goal":"must not resume after recovery"}, session["csrf_token"])
                need(status == (429 if index == 0 else 403), "restored admission stop failed")
                with app.runtimes.acquire(tenant_id=account["tenant"], workspace_id=account["workspace"]) as runtime:
                    need(runtime.tank.memory.get(account["memory"]).content == account["marker"], "hot memory missing")
                    hits = runtime.tank.ltm.retrieve(account["marker"], min_score=0)
                    need(any(hit["id"] == account["semantic"] and hit["content"] == account["marker"] + " vector"
                             for hit in hits), "vector/LTM memory missing")
                    need(runtime.tank.ltm.restore_from_cold(account["cold"]), "cold archive could not be restored")
                    full = runtime.tank.ltm._conn.execute("SELECT content FROM memory_entries WHERE id=?",
                                                         (account["cold"],)).fetchone()
                    need(full[0] == account["marker"] + " full cold archive content", "cold archive full text mismatch")
                    runs = runtime.tank.run_store.list_recent()
                    need(len(runs) == 1 and runs[0]["goal"] == history[0]["goal"], "run journal mismatch")
        need(not sends and ledger.snapshot() == metadata["budget"], "recovery caused spend or unlocked budget")
    return {"login_and_roles":True, "tenant_isolation":True, "history_and_runs":True,
            "hot_ltm_vector_cold_memory":True, "source_grant_loaded":True,
            "old_sessions_and_agent_tokens_revoked":True, "budget_and_replay_preserved":True,
            "blocked_owner_http_status":429, "provider_calls":0}


def child(phase, root, metadata):
    result = subprocess.run([sys.executable, "-X", "utf8", str(Path(__file__).resolve()),
        "--internal-phase", phase, "--internal-root", str(root), "--internal-metadata", str(metadata)],
        capture_output=True, text=True, encoding="utf-8", timeout=60)
    need(result.returncode == 0, "synthetic child failed: " + result.stderr[-2000:])
    line = next((line for line in reversed(result.stdout.splitlines()) if line.startswith("DRILL_JSON=")), None)
    need(line is not None, "synthetic child receipt missing")
    return json.loads(line.removeprefix("DRILL_JSON="))


def run_drill():
    with tempfile.TemporaryDirectory(prefix="tankai-synthetic-cold-drill-") as directory:
        base = Path(directory).resolve()
        source, destination, metadata = base/"source", base/"restored", base/"expected.json"
        child("seed", source, metadata)  # Process exit is the quiescence boundary.
        # No writer process remains. Include committed WAL before cold capture.
        for path in source.rglob("*"):
            if path.suffix in {".db", ".sqlite"}:
                with closing(sqlite3.connect(path)) as connection:
                    need(connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0,
                         "cold checkpoint is busy")
                    need(connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete",
                         "cold database did not leave WAL mode")
        files = inventory(source)
        expected = json.loads(metadata.read_bytes())
        required = {"auth.db", "monetary.sqlite", "approved-source.json"}
        for account in expected["accounts"]:
            prefix = workspace_path(source, account).relative_to(source).as_posix()
            required.update(prefix + "/" + name for name in
                ("memory.db", "ltm.db", "vectors.npz", "runs.jsonl", "web_history.jsonl", "cold/memory_archive.jsonl"))
        need(set(files) == required and inventory(source) == files, "incomplete or changing cold fixture")
        manifest = {name:digest(value) for name, value in files.items()}
        key = secrets.token_bytes(32)  # Ephemeral test key; never printed or stored.
        artifact = seal(files, key)
        artifact_file = base/"synthetic-recovery.aesgcm"
        artifact_file.write_bytes(artifact)
        pin = digest(artifact)
        need(source.parent == base and source.name == "source" and not source.is_symlink(), "unsafe synthetic cleanup")
        shutil.rmtree(source)
        need(not source.exists(), "synthetic source was not removed")
        restore(artifact_file.read_bytes(), key, pin, manifest, destination)
        first = child("verify", destination, metadata)
        second = child("verify", destination, metadata)
        need(first == second, "fresh-process recovery checks changed on second boot")
        return {"format":"tankai.synthetic-cold-recovery-drill.v1", "ok":True,
            "synthetic_only":True, "seed_llm_mode":"simulation",
            "writer_exited_before_capture":True, "source_removed_before_restore":True,
            "encrypted_artifact_sha256":pin, "manifest_sha256":digest(canonical(manifest)),
            "file_count":len(files), "plaintext_bytes":sum(map(len, files.values())),
            "encrypted_bytes":len(artifact), "fresh_verifier_processes":2, "checks":first,
            "production_host_verified":False, "live_writer_fencing_verified":False,
            "public_pilot_authorized":False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--internal-phase", choices=["seed", "verify"], help=argparse.SUPPRESS)
    parser.add_argument("--internal-root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--internal-metadata", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.internal_phase:
        need(args.internal_root is not None and args.internal_metadata is not None,
             "missing synthetic child context")
        base = args.internal_metadata.parent.resolve()
        need(base.name.startswith("tankai-synthetic-cold-drill-") and base.parent == Path(tempfile.gettempdir()).resolve()
             and args.internal_root.parent.resolve() == base
             and not args.internal_root.is_symlink() and not args.internal_metadata.is_symlink()
             and args.internal_metadata.name == "expected.json"
             and args.internal_root.name == ("source" if args.internal_phase == "seed" else "restored"),
             "internal child only accepts its isolated synthetic temporary directory")
        result = seed(args.internal_root, args.internal_metadata) if args.internal_phase == "seed" else verify(
            args.internal_root, args.internal_metadata)
        print("DRILL_JSON=" + json.dumps(result))
    else:
        result = run_drill()
        if args.report:
            with args.report.open("x", encoding="utf-8") as report:
                report.write(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
