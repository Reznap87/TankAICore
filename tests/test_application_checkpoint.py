"""Real filesystem/OS lock/SQLite/encryption with synthetic identities; no providers."""
from contextlib import closing
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from tankai.core.monetary_budget import BudgetDenied, MonetaryBudget, PilotPolicy, PriceQuote
from tankai.web import application_checkpoint as cp
from tankai.web import application_root as roots
from tankai.web.auth import AuthStore
from tankai.web.server import AppContext, Handler, ThreadedHTTPServer
from test_web_server import _configure, Client

NOW = 1_790_000_000
PASSWORD = "Synthetic-checkpoint-password-123"


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    root = tmp_path/"data"
    metadata = roots.initialize_root(root)
    _configure(monkeypatch, tmp_path)
    for key in ("TANKAI_AUTH_STATE_URL", "TANKAI_AUTH_STATE_TOKEN", "TANKAI_BOOTSTRAP_OWNER"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TANKAI_DEV_QUEUE_ENABLED", "0")
    monkeypatch.setenv("TANKAI_EMBEDDER", "hashing")
    from tankai.core import llm
    monkeypatch.setattr(llm, "_load_dotenv_if_available", lambda:None)
    original_connect = socket.socket.connect
    def local_only(sock, address):
        assert address[0] in {"127.0.0.1", "::1", "localhost"}, "external network is forbidden in checkpoint tests"
        return original_connect(sock, address)
    monkeypatch.setattr(socket.socket, "connect", local_only)
    app = AppContext.from_env("127.0.0.1")
    server = ThreadedHTTPServer(("127.0.0.1", 0), Handler, app=app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    accounts = []
    try:
        for index in range(2):
            email = f"checkpoint-{index}@example.test"
            owner, tenant, workspace = app.auth.create_user_with_tenant(email=email,
                password=PASSWORD, display_name="Synthetic", tenant_name="Synthetic")
            client = Client(f"http://127.0.0.1:{server.server_address[1]}")
            status, _, login = client.post("/api/auth/login", {"email":email, "password":PASSWORD})
            assert status == 200
            status, _, response = client.post("/api/run", {"goal":f"Schreibe synthetic-marker-{index}"}, csrf=login["csrf_token"])
            assert status == 200 and response["execution_mode"] == "simulation"
            with app.runtimes.acquire(tenant_id=tenant, workspace_id=workspace) as runtime:
                entry = runtime.tank.memory.add(f"memory-{index}", "synthetic").id
                runtime.tank.ltm.add_semantic(f"vector-{index}", source="synthetic")
                cold = runtime.tank.ltm.add_semantic(f"cold-{index}", source="synthetic")
                assert runtime.tank.ltm._move_to_cold(cold.id)
            accounts.append(dict(owner=owner, tenant=tenant, workspace=workspace, email=email, entry=entry))
        session = app.auth.authenticate(email=accounts[0]["email"], password=PASSWORD)
        agent = app.auth.create_service_agent(actor=session.context, name="Synthetic checkpoint agent")
        token = app.auth.create_agent_token(actor=session.context, agent_id=agent.agent_id,
            scopes=["jobs:read"], repository_ids=["00000000-0000-4000-8000-000000000101"])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
    policy = PilotPolicy(accounts[0]["owner"], NOW, NOW+86400)
    quote = PriceQuote("checkpoint-test", "synthetic", "synthetic", NOW, NOW+86400, 6000,
                       "https://example.test/price", "Synthetic fixed all-in ceiling")
    with MonetaryBudget.create(root/"monetary.sqlite", policy, clock=lambda:NOW) as ledger:
        ledger.begin_job("interrupted", owner_id=policy.owner_id)
        ledger.reserve("interrupted", "unknown", owner_id=policy.owner_id, quote=quote)
        ledger.settle("unknown", owner_id=policy.owner_id, actual_micro_eur=None)
        budget = ledger.snapshot()
    (root/"sources").mkdir()
    first = accounts[0]
    text = "Synthetic approved checkpoint source."
    grant = {"schema_version":1, "owner_id":first["owner"], "tenant_id":first["tenant"],
        "workspace_id":first["workspace"], "valid_from":NOW, "valid_until":NOW+86400,
        "approval_id":"checkpoint-test", "sources":[{"title":"Synthetic", "url":"https://example.test/source",
        "text":text, "sha256":hashlib.sha256(text.encode()).hexdigest()}]}
    source_name = f'sources/{first["workspace"]}.json'
    raw = json.dumps(grant).encode()
    (root/source_name).write_bytes(raw)
    return SimpleNamespace(root=root, metadata=metadata, policy=policy, accounts=accounts,
        budget=budget, quote=quote, session=session.token, token=token.token,
        pins={source_name:hashlib.sha256(raw).hexdigest()}, path=tmp_path/"checkpoint.enc", key=secrets.token_bytes(32))


def trusted(s):
    return dict(expected_root_id=s.metadata["root_id"], expected_policy=s.policy, expected_source_pins=s.pins)


def save(s):
    return cp.create_checkpoint(s.root, s.path, s.key, **trusted(s))


def restore(s, target, receipt):
    return cp.restore_checkpoint(s.path, target, s.key, expected_sha256=receipt["encrypted_sha256"], **trusted(s))


def test_common_generation_restores_accounts_memory_history_and_uncertain_budget(seeded, tmp_path, monkeypatch):
    s = seeded
    before = {p.relative_to(s.root).as_posix():p.read_bytes() for p in s.root.rglob("*") if p.is_file() and not p.name.endswith(("-wal", "-shm"))}
    receipt = save(s)
    assert receipt["files"] == 15 and receipt["persisted_ciphertext_verified"]
    assert not receipt["production_ready"] and not receipt["freshness_verified"]
    ciphertext = s.path.read_bytes()
    assert b"SQLite format 3" not in ciphertext and s.policy.owner_id.encode() not in ciphertext
    assert s.key.hex() not in str(receipt) and s.session not in str(receipt)
    assert cp.verify_checkpoint(s.path, s.key, expected_sha256=receipt["encrypted_sha256"], **trusted(s))["manifest_sha256"] == receipt["manifest_sha256"]
    target = tmp_path/"restored"
    result = restore(s, target, receipt)
    assert result["restored_state_verified"] and result["sessions_invalidated"] >= 3 and result["agent_tokens_invalidated"] == 1
    assert result["state"] == "staged-restore" and not result["web_startup_authorized"]
    assert not (target/roots.PENDING).exists()
    # A fresh interpreter verifies persisted state, rather than trusting cached objects.
    script = '''
import json, sqlite3, sys, socket
def no_network(*args, **kwargs): raise AssertionError('no network in fresh verifier')
socket.socket.connect=no_network
from pathlib import Path
from tankai.web.auth import AuthStore
from tankai.core.monetary_budget import MonetaryBudget, BudgetDenied, PriceQuote
from tankai.core.memory import Memory
from tankai.core.long_term_memory import LongTermMemory
root=Path(sys.argv[1]); expected=json.loads(sys.stdin.read()); store=AuthStore(root/'auth.db')
assert store.resolve_session(expected['session']) is None
assert store.resolve_agent_token(expected['token']) is None
for account in expected['accounts']:
    session=store.authenticate(email=account['email'],password=expected['password'])
    assert session is not None
    foreign=next(a for a in expected['accounts'] if a!=account)
    assert store.workspace_access(account['owner'],foreign['workspace']) is None
    workspace=root/'tenants'/account['tenant']/'workspaces'/account['workspace']
    memory=Memory(workspace/'memory.db'); assert memory.get(account['entry']) is not None; memory.close()
    ltm=LongTermMemory(db_path=workspace/'ltm.db',vector_path=workspace/'vectors.npz',cold_dir=workspace/'cold',embedder='hashing')
    assert len(ltm.vectors)>0; ltm.close()
    assert (workspace/'web_history.jsonl').read_text() and (workspace/'runs.jsonl').read_text()
    assert 'cold-' in (workspace/'cold'/'memory_archive.jsonl').read_text()
with MonetaryBudget(root/'monetary.sqlite',clock=lambda:expected['now']) as ledger:
    assert ledger.snapshot()==expected['budget']
    assert not ledger.reserve('interrupted','unknown',owner_id=ledger.policy.owner_id,quote=PriceQuote(**expected['quote'])).dispatch_allowed
    try: ledger.begin_job('new',owner_id=ledger.policy.owner_id)
    except BudgetDenied: pass
    else: raise AssertionError('uncertain job was released')
print('FRESH_PROCESS_RESTORE_OK')
'''
    from dataclasses import asdict
    completed = subprocess.run([sys.executable, "-c", script, str(target)],
        input=json.dumps(dict(session=s.session, token=s.token, accounts=s.accounts, password=PASSWORD,
                             now=NOW, budget=s.budget, quote=asdict(s.quote))), text=True, capture_output=True, timeout=30)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "FRESH_PROCESS_RESTORE_OK"
    # SQLite may checkpoint its WAL when the last reader closes; physical main
    # file bytes can change while the committed logical source remains intact.
    for name, value in before.items():
        if not name.endswith((".db", ".sqlite")):
            assert (s.root/name).read_bytes() == value
    with closing(sqlite3.connect(s.root/"auth.db")) as auth:
        assert auth.execute("SELECT COUNT(*) FROM sessions WHERE revoked_at IS NULL").fetchone()[0] >= 3
        assert auth.execute("SELECT COUNT(*) FROM agent_tokens WHERE revoked_at IS NULL").fetchone()[0] == 1
    with MonetaryBudget(s.root/"monetary.sqlite", clock=lambda:NOW) as original:
        assert original.snapshot() == s.budget
    monkeypatch.setenv("TANKAI_DATA_ROOT", str(target))
    monkeypatch.setenv("TANKAI_AUTH_DB", str(target/"auth.db"))
    with pytest.raises(roots.ApplicationRootError, match="staged"):
        AppContext.from_env("127.0.0.1")


def test_running_application_blocks_export_in_process_and_across_processes(seeded):
    s = seeded
    app = AppContext.from_env("127.0.0.1")
    try:
        with pytest.raises(cp.CheckpointError): save(s)
        child = subprocess.run([sys.executable, "-c",
            "from tankai.web.application_root import RootLease; import sys; RootLease(sys.argv[1])", str(s.root)],
            text=True, capture_output=True, timeout=10)
        assert child.returncode != 0
        assert not s.path.exists()
        with pytest.raises(roots.ApplicationRootError): AppContext.from_env("127.0.0.1")
    finally:
        app.close()
    assert save(s)["ok"]


def test_active_workspace_lease_retains_root_fence_until_close_retry(seeded):
    s = seeded
    app = AppContext.from_env("127.0.0.1")
    account = s.accounts[0]
    try:
        with app.runtimes.acquire(tenant_id=account["tenant"], workspace_id=account["workspace"]):
            with pytest.raises(RuntimeError, match="active workspace"): app.close()
            with pytest.raises(cp.CheckpointError): save(s)
        app.close()
        assert save(s)["ok"]
    finally:
        app.close()


def test_wal_committed_pages_are_captured_and_live_write_transaction_blocks(seeded):
    s = seeded
    with closing(sqlite3.connect(s.root/"auth.db", isolation_level=None)) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("UPDATE users SET display_name='Committed WAL marker'")
        assert Path(str(s.root/"auth.db")+"-wal").stat().st_size > 0
        db.execute("BEGIN IMMEDIATE")
        with pytest.raises(cp.CheckpointError): save(s)
        assert not s.path.exists()
        db.execute("ROLLBACK")
        receipt = save(s)
        files, _, _ = cp._decode(s.path, s.key, receipt["encrypted_sha256"], s.metadata["root_id"], s.policy, s.pins)
        with closing(cp._database(files["auth.db"])) as image:
            assert image.execute("SELECT DISTINCT display_name FROM users").fetchall() == [("Committed WAL marker",)]


@pytest.mark.parametrize("failure", ["wrong-key", "wrong-pin", "wrong-root", "wrong-policy", "wrong-source", "tampered", "truncated"])
def test_invalid_authentication_never_creates_restore_destination(seeded, tmp_path, failure):
    s = seeded
    receipt = save(s)
    options = dict(**trusted(s), expected_sha256=receipt["encrypted_sha256"])
    key = s.key
    if failure == "wrong-key": key = secrets.token_bytes(32)
    if failure == "wrong-pin": options["expected_sha256"] = "0"*64
    if failure == "wrong-root": options["expected_root_id"] = "00000000-0000-4000-8000-000000000001"
    if failure == "wrong-policy": options["expected_policy"] = replace(s.policy, total_micro_eur=9_000_000)
    if failure == "wrong-source": options["expected_source_pins"] = {name:"0"*64 for name in s.pins}
    if failure in {"tampered", "truncated"}:
        data = s.path.read_bytes()
        data = data[:-20] if failure == "truncated" else data[:-1]+bytes([data[-1]^1])
        s.path.write_bytes(data)
        options["expected_sha256"] = hashlib.sha256(data).hexdigest()
    target = tmp_path/"invalid"
    with pytest.raises(cp.CheckpointError): cp.restore_checkpoint(s.path, target, key, **options)
    assert not target.exists()


@pytest.mark.parametrize("extra", ["csv.db", "forgotten.json", "auth.db-journal/child", "tenants/unknown/secret", "sources/other.json"])
def test_unknown_files_and_directories_block_export(seeded, extra):
    s = seeded
    target = s.root/extra
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("synthetic")
    with pytest.raises(cp.CheckpointError): save(s)
    assert not s.path.exists()


def test_source_grant_and_workspace_membership_are_required(seeded):
    s = seeded
    with closing(sqlite3.connect(s.root/"auth.db")) as auth:
        with auth:
            auth.execute("DELETE FROM memberships WHERE user_id=?", (s.policy.owner_id,))
    with pytest.raises(cp.CheckpointError): save(s)
    assert not s.path.exists()


def test_missing_workspace_database_blocks_export(seeded):
    s = seeded
    path = next(s.root.glob("tenants/*/workspaces/*/memory.db"))
    path.unlink()
    with pytest.raises(cp.CheckpointError): save(s)


def test_never_overwrite_backup_or_restore_and_never_adopt_existing_root(seeded, tmp_path):
    s = seeded
    receipt = save(s)
    original = s.path.read_bytes()
    with pytest.raises(cp.CheckpointError): save(s)
    assert s.path.read_bytes() == original
    target = tmp_path/"existing"
    target.mkdir(); (target/"sentinel").write_bytes(b"keep")
    with pytest.raises(cp.CheckpointError): restore(s, target, receipt)
    with pytest.raises(FileExistsError): roots.initialize_root(target)
    assert (target/"sentinel").read_bytes() == b"keep"


def test_link_alias_blocks_export(seeded):
    s = seeded
    source = s.root/next(iter(s.pins))
    alias = s.root.parent/"outside-alias"
    os.link(source, alias)
    with pytest.raises(cp.CheckpointError): save(s)


def test_file_changed_after_capture_fails_closed(seeded, monkeypatch):
    s = seeded
    original = cp._validate
    def changed(*args):
        original(*args)
        path = next(s.root.glob("tenants/*/workspaces/*/web_history.jsonl"))
        path.write_text('{"changed":true}\n')
    monkeypatch.setattr(cp, "_validate", changed)
    with pytest.raises(cp.CheckpointError, match="changed"): save(s)
    assert not s.path.exists()


def test_partial_restore_stays_blocked_and_cannot_be_reused(seeded, tmp_path, monkeypatch):
    s = seeded
    receipt = save(s)
    target = tmp_path/"partial"
    original = cp.write_new
    def failed(path, data):
        if path == target/"monetary.sqlite": raise OSError("synthetic write failure")
        return original(path, data)
    monkeypatch.setattr(cp, "write_new", failed)
    with pytest.raises(cp.CheckpointError): restore(s, target, receipt)
    assert (target/roots.PENDING).exists()
    monkeypatch.setenv("TANKAI_DATA_ROOT", str(target))
    monkeypatch.setenv("TANKAI_AUTH_DB", str(target/"auth.db"))
    with pytest.raises(roots.ApplicationRootError): AppContext.from_env("127.0.0.1")
    with pytest.raises(cp.CheckpointError): restore(s, target, receipt)


@pytest.mark.parametrize("name,value", [("TANKAI_AUTH_DB", "external.db"), ("TANKAI_DEV_QUEUE_ENABLED", "1"),
    ("TANKAI_AUTH_STATE_URL", "https://example.test/store"), ("TANKAI_EMBEDDER", "openai"), ("TANKAI_AUTH_MODE", "disabled")])
def test_managed_web_rejects_external_or_uncovered_storage_before_writes(seeded, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(roots.ApplicationRootError): AppContext.from_env("127.0.0.1")
    # Failed initialization released its lease.
    with roots.RootLease(seeded.root): pass


def test_vector_header_cannot_request_unbounded_allocation():
    import numpy as np
    import zipfile
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in ("format_version", "dim", "ids", "vectors", "metadatas"):
            header = io.BytesIO()
            np.lib.format.write_array_header_1_0(header, dict(descr="<f4", fortran_order=False, shape=(10**12,)))
            archive.writestr(name+".npy", header.getvalue())
    with pytest.raises(cp.CheckpointError): cp._vectors(buffer.getvalue())


def test_export_holds_process_fence_until_persisted_verification(seeded, monkeypatch):
    s = seeded
    original = cp._decode
    checked = []
    def guarded(*args):
        with pytest.raises(roots.ApplicationRootError): AppContext.from_env("127.0.0.1")
        checked.append(True)
        return original(*args)
    monkeypatch.setattr(cp, "_decode", guarded)
    assert save(s)["ok"] and checked == [True]


@pytest.mark.parametrize("change", ["replace", "sidecar"])
def test_restore_rechecks_file_identity_and_sidecars_after_directory_sync(seeded, tmp_path, monkeypatch, change):
    s = seeded
    receipt = save(s)
    target = tmp_path/"tamper-on-sync"
    original = cp._sync_directory
    fired = []
    def tamper(path):
        original(path)
        if Path(path) == target and (target/roots.MARKER).exists() and not fired:
            fired.append(True)
            if change == "replace":
                file = target/"monetary.sqlite"
                renamed = target/"retained-original"
                file.rename(renamed)
                file.write_bytes(renamed.read_bytes())
                renamed.unlink()
            else:
                (target/"auth.db-wal").write_bytes(b"synthetic unexpected WAL")
    monkeypatch.setattr(cp, "_sync_directory", tamper)
    with pytest.raises(cp.CheckpointError): restore(s, target, receipt)
    assert fired and (target/roots.PENDING).exists()


def test_failed_application_initialization_releases_fence(seeded, monkeypatch):
    s = seeded
    monkeypatch.setenv("TANKAI_SESSION_HOURS", "invalid")
    with pytest.raises(RuntimeError): AppContext.from_env("127.0.0.1")
    with roots.RootLease(s.root): pass


def test_lock_identity_replacement_never_passes_check(tmp_path):
    root = tmp_path/"root"
    roots.initialize_root(root)
    with roots.RootLease(root) as lease:
        try:
            (root/roots.LOCK).rename(root/"old-lock")
        except PermissionError:
            # Windows may fence rename itself while the CRT handle is open.
            return
        (root/roots.LOCK).write_bytes(b"0")
        with pytest.raises(roots.ApplicationRootError): lease.check()


def test_new_root_initialization_incomplete_marker_blocks_web(tmp_path, monkeypatch):
    root = tmp_path/"incomplete"
    root.mkdir()
    (root/roots.PENDING).write_bytes(b"pending")
    monkeypatch.setenv("TANKAI_DATA_ROOT", str(root))
    with pytest.raises(roots.ApplicationRootError, match="incomplete"): AppContext.from_env("127.0.0.1")
    assert not (root/"auth.db").exists()


def test_managed_root_rejects_directory_aliases(tmp_path):
    root = tmp_path/"root"
    roots.initialize_root(root)
    alias = tmp_path/"alias"
    try:
        alias.symlink_to(root, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation requires Windows privilege")
    with pytest.raises(roots.ApplicationRootError): roots.RootLease(alias)


def test_mutated_ciphertext_during_persisted_validation_cannot_receive_success(seeded, monkeypatch):
    s = seeded
    original = cp._decode
    def tampered(*args):
        result = original(*args)
        data = s.path.read_bytes()
        s.path.write_bytes(data[:-1]+bytes([data[-1]^1]))
        return result
    monkeypatch.setattr(cp, "_decode", tampered)
    with pytest.raises(cp.CheckpointError, match="persisted"): save(s)


def test_vector_zero_width_dtype_is_rejected_before_numpy_allocation():
    import numpy as np
    import zipfile
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in ("format_version", "dim", "ids", "vectors", "metadatas"):
            header = io.BytesIO()
            np.lib.format.write_array_header_1_0(header, dict(descr="<U0", fortran_order=False, shape=(8_000_000,)))
            archive.writestr(name+".npy", header.getvalue())
    with pytest.raises(cp.CheckpointError): cp._vectors(buffer.getvalue())
