"""Account and CSV recovery from one encrypted, coordinated offline package."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import stat
import subprocess
import sys
from urllib.request import Request
import threading
from uuid import uuid4

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import pytest

from tankai.web import recovery_backup as recovery
from tankai.web import server as web_server
from tankai.web.auth import AuthStore
from tankai.web.file_jobs import FileJobStore
from test_file_backup import key_file
from test_file_jobs import opts, upload
from test_web_server import Client


@pytest.fixture
def source(tmp_path):
    root = tmp_path / 'source'
    auth = AuthStore(root / 'auth.db')
    for name in ('owner', 'other'):
        auth.create_user_with_tenant(email=name + '@example.test', password='Recovery-test-password-123',
                                    display_name=name, tenant_name=name)
    session = auth.authenticate(email='owner@example.test', password='Recovery-test-password-123')
    files = FileJobStore(root / 'csv-jobs.db')
    draft = files.create(session.context, upload(name='pending.csv'))
    draft = files.update(session.context, draft['id'], {'revision': 1, 'options': opts(trim=True)})
    done = files.create(session.context, upload(name='finished.csv'))
    files.update(session.context, done['id'], {'revision': 1}, complete=True)
    agent = auth.create_service_agent(actor=session.context, name='Recovery fixture')
    token = auth.create_agent_token(actor=session.context, agent_id=agent.agent_id,
                                    scopes=['jobs:read'], repository_ids=[str(uuid4())])
    return root, auth, files, session, draft, done, token


def test_identical_replacement_of_saved_backup_is_rejected(
    source, tmp_path, monkeypatch,
):
    root, *_ = source
    key = secrets.token_bytes(32)
    destination = tmp_path / 'replaced-backup.enc'
    original = recovery.csv_backup._write_new

    def replace_with_identical_bytes(path, data):
        identity = original(path, data)
        if path == destination:
            path.unlink()
            path.write_bytes(data)
        return identity

    monkeypatch.setattr(recovery.csv_backup, '_write_new', replace_with_identical_bytes)
    with pytest.raises(recovery.csv_backup.FileBackupError, match='gespeicherte Sicherung.*ersetzt'):
        recovery.create_backup(root, destination, key)
    assert destination.exists()


def test_pair_recovers_password_hashes_roles_choices_and_deliveries(source, tmp_path):
    root, auth, files, session, draft, done, agent_token = source
    assert auth.consume_provider_call_event(user_id=session.context.user_id, provider='fixture', limit=1, window_seconds=3600) is None
    key = secrets.token_bytes(32)
    encrypted = tmp_path / 'pair.enc'
    receipt = recovery.create_backup(root, encrypted, key)
    assert receipt['users'] == receipt['workspaces'] == receipt['jobs'] == 2
    assert receipt['coordinatedWriteBarrier'] and receipt['persistedCiphertextVerified']
    assert recovery.verify_backup(encrypted, key)['authSnapshotSha256'] == receipt['authSnapshotSha256']
    destination = tmp_path / 'fresh'
    restored_receipt = recovery.restore_backup(encrypted, destination, key)
    assert restored_receipt['freshLoginRequired']
    assert restored_receipt['sessionsInvalidated'] == restored_receipt['agentTokensInvalidated'] == 1
    assert not (destination / recovery.RESTORE_PENDING_FILE).exists()
    assert json.loads((destination / recovery.RECEIPT_FILE).read_text()) == restored_receipt
    restored_auth = AuthStore(destination / 'auth.db')
    assert restored_auth.resolve_session(session.token) is None
    assert restored_auth.resolve_agent_token(agent_token.token) is None
    assert auth.resolve_session(session.token, touch=False) is not None
    assert auth.resolve_agent_token(agent_token.token) is not None
    new_session = restored_auth.authenticate(email='owner@example.test', password='Recovery-test-password-123')
    assert new_session.context.user_id == session.context.user_id
    assert new_session.context.role == session.context.role == 'owner'
    assert restored_auth.consume_provider_call_event(user_id=new_session.context.user_id, provider='fixture', limit=1, window_seconds=3600) is not None
    restored_files = FileJobStore(destination / 'csv-jobs.db')
    assert restored_files.get(new_session.context, draft['id']) == files.get(session.context, draft['id'])
    restored_files.update(new_session.context, draft['id'], {'revision': draft['revision']}, complete=True)
    for kind in ('csv', 'report'):
        assert restored_files.download(new_session.context, done['id'], kind) == files.download(session.context, done['id'], kind)
    other = restored_auth.authenticate(email='other@example.test', password='Recovery-test-password-123')
    assert restored_files.list(other.context) == []


def test_restore_rechecks_artifacts_after_pending_marker_durability(
    source, tmp_path, monkeypatch,
):
    root, *_ = source
    key = secrets.token_bytes(32)
    encrypted, destination = tmp_path / "pair.enc", tmp_path / "final-check"
    recovery.create_backup(root, encrypted, key)
    original = recovery.csv_backup._sync_directory
    preserved = tmp_path / "exclusive-auth.db"
    replaced = False

    def replace_after_final_sync(directory):
        nonlocal replaced
        original(directory)
        pending = destination / recovery.RESTORE_PENDING_FILE
        auth = destination / "auth.db"
        if directory == destination and auth.exists() and not pending.exists() and not replaced:
            replaced = True
            auth.rename(preserved)
            auth.write_bytes(preserved.read_bytes())

    monkeypatch.setattr(recovery.csv_backup, "_sync_directory", replace_after_final_sync)
    with pytest.raises(
        recovery.csv_backup.FileBackupError,
        match="Kontendatenbank wurde vor Abschluss ersetzt",
    ):
        recovery.restore_backup(encrypted, destination, key)
    assert replaced
    assert (destination / "auth.db").read_bytes() == preserved.read_bytes()


def test_fresh_app_uses_both_recovered_databases_and_requires_new_login(source, tmp_path, monkeypatch):
    root, _, files, old_session, draft, done, _ = source
    key = secrets.token_bytes(32)
    encrypted, destination = tmp_path / 'pair.enc', tmp_path / 'new-app'
    recovery.create_backup(root, encrypted, key)
    recovery.restore_backup(encrypted, destination, key)
    monkeypatch.setenv('TANKAI_AUTH_MODE', 'session')
    monkeypatch.setenv('TANKAI_DATA_ROOT', str(destination))
    monkeypatch.setenv('TANKAI_AUTH_DB', str(destination / 'auth.db'))
    monkeypatch.setenv('TANKAI_COOKIE_SECURE', '0')
    monkeypatch.setenv('TANKAI_FILE_JOBS_ENABLED', '1')
    monkeypatch.setenv('TANKAI_LLM', 'mock')
    monkeypatch.setenv('TANKAI_EMBEDDER', 'hashing')
    for key_name in ('TANKAI_AUTH_STATE_URL', 'TANKAI_AUTH_STATE_TOKEN', 'TANKAI_BOOTSTRAP_OWNER'):
        monkeypatch.delenv(key_name, raising=False)
    app = web_server.AppContext.from_env('127.0.0.1')
    assert app.auth.path.parent == app.file_jobs.path.parent == destination
    def forbid(**kwargs):
        raise AssertionError('Recovery verification attempted a model operation')
    monkeypatch.setattr(app.runtimes, 'acquire', forbid)
    server = web_server.ThreadedHTTPServer(('127.0.0.1', 0), web_server.Handler, app=app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f'http://127.0.0.1:{server.server_address[1]}'
        client = Client(base)
        assert client.get('/api/auth/me', headers={'Cookie': 'tankai_session=' + old_session.token})[0] == 401
        status, _, me = client.post('/api/auth/login', {'email': 'owner@example.test', 'password': 'Recovery-test-password-123'})
        assert status == 200 and me['user']['id'] == old_session.context.user_id
        client.opener.addheaders.append(('X-TankAI-Workspace', me['workspace']['id']))
        assert client.get('/api/file-jobs/' + draft['id'])[2]['options']['trim']
        assert client.post('/api/file-jobs/' + draft['id'] + '/complete', {'revision': draft['revision']}, csrf=me['csrf_token'])[0] == 200
        with client.opener.open(Request(base + '/api/file-jobs/' + done['id'] + '/csv'), timeout=5) as response:
            assert response.read() == files.download(old_session.context, done['id'], 'csv')[0]
        assert client.get('/api/file-jobs/' + done['id'] + '/report')[2]['jobId'] == done['id']
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def test_both_write_barriers_are_held_and_released(source, tmp_path, monkeypatch):
    root, *_ = source
    original = recovery.csv_backup._sqlite_snapshot
    observed = []
    def checking_snapshot(path, *args, **kwargs):
        for name in ('auth.db', 'csv-jobs.db'):
            connection = sqlite3.connect(root / name, timeout=0)
            try:
                with pytest.raises(sqlite3.OperationalError, match='locked'):
                    connection.execute('BEGIN IMMEDIATE')
                observed.append(name)
            finally:
                connection.close()
        return original(path, *args, **kwargs)
    monkeypatch.setattr(recovery.csv_backup, '_sqlite_snapshot', checking_snapshot)
    recovery.create_backup(root, tmp_path / 'barriers.enc', secrets.token_bytes(32))
    assert observed == ['auth.db', 'csv-jobs.db'] * 2
    for name in ('auth.db', 'csv-jobs.db'):
        connection = sqlite3.connect(root / name, timeout=0)
        connection.execute('BEGIN IMMEDIATE')
        connection.rollback()
        connection.close()


@pytest.mark.skipif(os.name == 'nt', reason='POSIX directory replacement contract')
def test_source_replacement_after_write_barriers_is_rejected(
    source, tmp_path, monkeypatch,
):
    root, *_ = source
    original = recovery._write_barriers
    moved = tmp_path / 'reserved-source'

    @contextmanager
    def replace_root_after_reservations(paths):
        with original(paths) as bound_sources:
            root.rename(moved)
            root.mkdir()
            for item in moved.iterdir():
                if item.is_file():
                    shutil.copy2(item, root / item.name)
            yield bound_sources

    monkeypatch.setattr(recovery, '_write_barriers', replace_root_after_reservations)
    output = tmp_path / 'must-not-exist.enc'
    with pytest.raises(
        recovery.csv_backup.FileBackupError, match='Schreibbarriere',
    ):
        recovery.create_backup(root, output, secrets.token_bytes(32))
    assert not output.exists()


def test_lock_failure_releases_first_reservation_and_creates_no_output(source, tmp_path, monkeypatch):
    root, *_ = source
    monkeypatch.setattr(recovery, 'WRITE_BARRIER_SECONDS', 0.02)
    busy = sqlite3.connect(root / 'csv-jobs.db')
    try:
        busy.execute('BEGIN IMMEDIATE')
        with pytest.raises(sqlite3.OperationalError):
            recovery.create_backup(root, tmp_path / 'failed.enc', secrets.token_bytes(32))
        assert not (tmp_path / 'failed.enc').exists()
        first = sqlite3.connect(root / 'auth.db', timeout=0)
        first.execute('BEGIN IMMEDIATE')
        first.rollback()
        first.close()
    finally:
        busy.rollback()
        busy.close()


@pytest.mark.parametrize('alteration', ['wrong-key', 'header', 'nonce', 'tag', 'truncated', 'appended'])
def test_bad_encrypted_package_never_creates_a_destination(source, tmp_path, alteration):
    root, *_ = source
    key = secrets.token_bytes(32)
    encrypted = tmp_path / 'pair.enc'
    recovery.create_backup(root, encrypted, key)
    data = bytearray(encrypted.read_bytes())
    if alteration == 'wrong-key':
        key = secrets.token_bytes(32)
    elif alteration == 'header':
        data[0] ^= 1
    elif alteration == 'nonce':
        data[len(recovery.MAGIC)] ^= 1
    elif alteration == 'tag':
        data[-1] ^= 1
    elif alteration == 'truncated':
        data = data[:-1]
    else:
        data += b'extra'
    encrypted.write_bytes(data)
    with pytest.raises(recovery.csv_backup.FileBackupError):
        recovery.restore_backup(encrypted, tmp_path / 'not-created', key)
    assert not (tmp_path / 'not-created').exists()


def test_unknown_owner_or_tenant_mismatch_rejects_the_pair(source, tmp_path):
    root, _, files, session, *_ = source
    connection = sqlite3.connect(files.path)
    try:
        connection.execute('UPDATE csv_jobs SET tenant=?', ('00000000-0000-4000-8000-000000000001',))
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(recovery.csv_backup.FileBackupError, match='zuordnung'):
        recovery.create_backup(root, tmp_path / 'mismatch.enc', secrets.token_bytes(32))
    assert not (tmp_path / 'mismatch.enc').exists()


def test_external_auth_snapshot_cannot_replace_restored_revocations(source, tmp_path, monkeypatch):
    root, *_ = source
    key = secrets.token_bytes(32)
    encrypted, destination = tmp_path / 'pair.enc', tmp_path / 'fresh'
    recovery.create_backup(root, encrypted, key)
    recovery.restore_backup(encrypted, destination, key)
    before = (destination / 'auth.db').read_bytes()
    monkeypatch.setenv('TANKAI_DATA_ROOT', str(destination))
    monkeypatch.setenv('TANKAI_AUTH_MODE', 'session')
    monkeypatch.setenv('TANKAI_AUTH_STATE_URL', 'https://not-contacted.example.test/auth')
    monkeypatch.setenv('TANKAI_AUTH_STATE_TOKEN', 'not-used-in-this-test')
    with pytest.raises(RuntimeError, match='externen Kontensicherung'):
        web_server.AppContext.from_env('127.0.0.1')
    assert (destination / 'auth.db').read_bytes() == before


@pytest.mark.parametrize('problem', ['external-auth-db', 'missing-auth', 'missing-csv'])
def test_recovered_root_cannot_silently_mix_old_or_missing_databases(source, tmp_path, monkeypatch, problem):
    root, *_ = source
    key, encrypted, destination = secrets.token_bytes(32), tmp_path / 'pair.enc', tmp_path / 'new'
    recovery.create_backup(root, encrypted, key)
    recovery.restore_backup(encrypted, destination, key)
    monkeypatch.setenv('TANKAI_DATA_ROOT', str(destination))
    monkeypatch.setenv('TANKAI_AUTH_DB', str((root if problem == 'external-auth-db' else destination) / 'auth.db'))
    monkeypatch.setenv('TANKAI_AUTH_MODE', 'session')
    monkeypatch.delenv('TANKAI_AUTH_STATE_URL', raising=False)
    monkeypatch.delenv('TANKAI_AUTH_STATE_TOKEN', raising=False)
    if problem == 'missing-auth':
        (destination / 'auth.db').unlink()
    elif problem == 'missing-csv':
        (destination / 'csv-jobs.db').unlink()
    with pytest.raises(RuntimeError, match='gemeinsam'):
        web_server.AppContext.from_env('127.0.0.1')
    if problem == 'missing-auth':
        assert not (destination / 'auth.db').exists()
    elif problem == 'missing-csv':
        assert not (destination / 'csv-jobs.db').exists()


@pytest.mark.parametrize('problem', ['length-mismatch', 'extra-bytes', 'unsupported-auth-schema', 'csv-only'])
def test_authenticated_but_invalid_members_are_rejected_before_target_creation(source, tmp_path, problem):
    root, *_ = source
    payload, _ = recovery._snapshot_pair(root)
    key, nonce = secrets.token_bytes(32), secrets.token_bytes(12)
    if problem == 'length-mismatch':
        payload = recovery.LENGTHS.pack(1, 1) + payload[recovery.LENGTHS.size:]
    elif problem == 'extra-bytes':
        payload += b'extra'
    elif problem == 'unsupported-auth-schema':
        auth_data, csv_data, _ = recovery._decode_payload(payload)
        db = sqlite3.connect(':memory:')
        try:
            db.deserialize(auth_data)
            db.execute("UPDATE auth_meta SET value='999' WHERE key='schema_version'")
            db.commit()
            auth_data = db.serialize()
        finally:
            db.close()
        payload = recovery.LENGTHS.pack(len(auth_data), len(csv_data)) + auth_data + csv_data
    else:
        encrypted = tmp_path / 'csv-only.enc'
        recovery.csv_backup.create_backup(root / 'csv-jobs.db', encrypted, key)
    if problem != 'csv-only':
        encrypted = tmp_path / 'invalid.enc'
        encrypted.write_bytes(recovery.MAGIC + nonce + AESGCM(key).encrypt(nonce, payload, recovery.AAD))
    with pytest.raises((RuntimeError, recovery.csv_backup.FileBackupError)):
        recovery.restore_backup(encrypted, tmp_path / 'not-created', key)
    assert not (tmp_path / 'not-created').exists()


def test_missing_source_does_not_create_databases(tmp_path):
    root = tmp_path / 'empty'
    root.mkdir()
    with pytest.raises(recovery.csv_backup.FileBackupError):
        recovery.create_backup(root, tmp_path / 'not-created.enc', secrets.token_bytes(32))
    assert list(root.iterdir()) == []


@pytest.mark.skipif(os.name == 'nt', reason='POSIX hard-link contract')
@pytest.mark.parametrize('database', ['auth.db', 'csv-jobs.db'])
def test_hard_linked_pair_source_database_is_rejected(source, tmp_path, database):
    root, *_ = source
    alias = tmp_path / f'{database}.alias'
    try:
        os.link(root / database, alias)
    except OSError:
        pytest.skip('Creating hard links is unavailable on this filesystem')

    output = tmp_path / 'must-not-exist.enc'
    with pytest.raises(recovery.csv_backup.FileBackupError, match='Hardlink'):
        recovery.create_backup(root, output, secrets.token_bytes(32))
    assert not output.exists()


def test_disabled_account_and_revoked_membership_remain_denied(source, tmp_path):
    root, auth, _, session, *_ = source
    db = sqlite3.connect(auth.path)
    try:
        db.execute('UPDATE users SET is_active=0 WHERE id=?', (session.context.user_id,))
        db.execute('DELETE FROM memberships WHERE user_id=?', (session.context.user_id,))
        db.commit()
    finally:
        db.close()
    key, encrypted = secrets.token_bytes(32), tmp_path / 'pair.enc'
    recovery.create_backup(root, encrypted, key)
    recovery.restore_backup(encrypted, tmp_path / 'restored', key)
    restored = AuthStore(tmp_path / 'restored/auth.db')
    assert restored.authenticate(email='owner@example.test', password='Recovery-test-password-123') is None
    assert restored.resolve_session(session.token) is None


def test_existing_target_and_incomplete_restore_are_not_started(source, tmp_path, monkeypatch):
    root, *_ = source
    key = secrets.token_bytes(32)
    encrypted = tmp_path / 'pair.enc'
    recovery.create_backup(root, encrypted, key)
    existing = tmp_path / 'existing'
    existing.mkdir()
    (existing / 'keep.txt').write_bytes(b'keep')
    with pytest.raises(FileExistsError):
        recovery.restore_backup(encrypted, existing, key)
    assert (existing / 'keep.txt').read_bytes() == b'keep'
    original = recovery.csv_backup._write_new
    def fail_second_database(path, data):
        if path.name == 'csv-jobs.db':
            raise OSError('synthetic disk failure')
        return original(path, data)
    monkeypatch.setattr(recovery.csv_backup, '_write_new', fail_second_database)
    failed = tmp_path / 'failed'
    with pytest.raises(OSError):
        recovery.restore_backup(encrypted, failed, key)
    assert (failed / recovery.RESTORE_PENDING_FILE).exists()
    assert not (failed / recovery.RECEIPT_FILE).exists()
    monkeypatch.setenv('TANKAI_DATA_ROOT', str(failed))
    monkeypatch.setenv('TANKAI_AUTH_MODE', 'session')
    with pytest.raises(RuntimeError, match='nicht abgeschlossen'):
        web_server.AppContext.from_env('127.0.0.1')


@pytest.mark.skipif(recovery.csv_backup.os.name == 'nt',
                    reason='Windows does not expose directory fsync through Python')
def test_success_is_reported_only_after_recovery_metadata_is_durable(source, tmp_path, monkeypatch):
    root, *_ = source
    key = secrets.token_bytes(32)
    encrypted = tmp_path / 'pair.enc'
    recovery.create_backup(root, encrypted, key)
    observed = []
    real_fsync = recovery.csv_backup.os.fsync

    def recording_fsync(descriptor):
        observed.append(stat.S_ISDIR(recovery.csv_backup.os.fstat(descriptor).st_mode))
        return real_fsync(descriptor)

    monkeypatch.setattr(recovery.csv_backup.os, 'fsync', recording_fsync)
    recovery.restore_backup(encrypted, tmp_path / 'durable', key)
    # Parent mkdir, completed database set and receipt/marker transition are
    # separate metadata durability boundaries.
    assert observed.count(True) >= 3


def test_corrupted_recovery_receipt_keeps_pending_marker(source, tmp_path, monkeypatch):
    root, *_ = source
    key = secrets.token_bytes(32)
    encrypted = tmp_path / 'pair.enc'
    recovery.create_backup(root, encrypted, key)
    original = recovery.csv_backup._write_new

    def corrupt_receipt(path, data):
        identity = original(path, data)
        if path.name == recovery.RECEIPT_FILE:
            path.write_bytes(b'{"ok":false}\n')
        return identity

    monkeypatch.setattr(recovery.csv_backup, '_write_new', corrupt_receipt)
    destination = tmp_path / 'corrupt-receipt'
    with pytest.raises(recovery.csv_backup.FileBackupError, match='Receipt'):
        recovery.restore_backup(encrypted, destination, key)
    assert (destination / recovery.RESTORE_PENDING_FILE).exists()
    assert (destination / recovery.RECEIPT_FILE).read_bytes() == b'{"ok":false}\n'


def test_database_changed_after_receipt_check_keeps_pending_marker(
    source, tmp_path, monkeypatch,
):
    root, *_ = source
    key = secrets.token_bytes(32)
    encrypted = tmp_path / 'pair.enc'
    recovery.create_backup(root, encrypted, key)
    original = recovery.csv_backup._read_regular

    def change_auth_after_receipt_check(path, limit, **kwargs):
        data = original(path, limit, **kwargs)
        if path.name == recovery.RECEIPT_FILE:
            auth_path = path.parent / 'auth.db'
            changed = bytearray(auth_path.read_bytes())
            changed[-1] ^= 1
            auth_path.write_bytes(changed)
        return data

    monkeypatch.setattr(recovery.csv_backup, '_read_regular', change_auth_after_receipt_check)
    destination = tmp_path / 'changed-after-receipt'
    with pytest.raises(recovery.csv_backup.FileBackupError, match='Kontendatenbank'):
        recovery.restore_backup(encrypted, destination, key)
    assert (destination / recovery.RESTORE_PENDING_FILE).exists()


def test_identical_replacement_receipt_keeps_pending_marker(source, tmp_path, monkeypatch):
    root, *_ = source
    key = secrets.token_bytes(32)
    encrypted = tmp_path / 'pair.enc'
    recovery.create_backup(root, encrypted, key)
    original = recovery.csv_backup._write_new

    def replace_receipt_with_identical_bytes(path, data):
        identity = original(path, data)
        if path.name == recovery.RECEIPT_FILE:
            path.unlink()
            path.write_bytes(data)
        return identity

    monkeypatch.setattr(recovery.csv_backup, '_write_new', replace_receipt_with_identical_bytes)
    destination = tmp_path / 'replaced-receipt'
    with pytest.raises(recovery.csv_backup.FileBackupError, match='Receipt.*ersetzt'):
        recovery.restore_backup(encrypted, destination, key)
    assert (destination / recovery.RESTORE_PENDING_FILE).exists()


def test_identical_replacement_during_receipt_read_keeps_pending_marker(
    source, tmp_path, monkeypatch,
):
    root, *_ = source
    key = secrets.token_bytes(32)
    encrypted = tmp_path / 'pair.enc'
    recovery.create_backup(root, encrypted, key)
    original = recovery.csv_backup._read_regular

    def replace_receipt_after_read(path, limit, **kwargs):
        data = original(path, limit, **kwargs)
        if path.name == recovery.RECEIPT_FILE:
            path.unlink()
            path.write_bytes(data)
        return data

    monkeypatch.setattr(recovery.csv_backup, '_read_regular', replace_receipt_after_read)
    destination = tmp_path / 'replaced-during-read'
    with pytest.raises(recovery.csv_backup.FileBackupError, match='Receipt.*ersetzt'):
        recovery.restore_backup(encrypted, destination, key)
    assert (destination / recovery.RESTORE_PENDING_FILE).exists()


@pytest.mark.skipif(os.name == 'nt', reason='POSIX pending-marker identity contract')
@pytest.mark.parametrize('alteration', ['missing', 'replaced', 'hard-linked'])
def test_changed_pending_marker_is_preserved_and_blocks_success(
    source, tmp_path, monkeypatch, alteration,
):
    root, *_ = source
    key = secrets.token_bytes(32)
    encrypted = tmp_path / 'pair.enc'
    recovery.create_backup(root, encrypted, key)
    original = recovery.csv_backup._read_regular
    replacement = b'foreign recovery marker\n'

    def replace_marker_after_receipt_check(path, limit, **kwargs):
        data = original(path, limit, **kwargs)
        if path.name == recovery.RECEIPT_FILE:
            pending = path.parent / recovery.RESTORE_PENDING_FILE
            foreign = path.parent / 'foreign-marker.tmp'
            if alteration != 'missing':
                foreign.write_bytes(replacement)
            pending.unlink()
            if alteration != 'missing':
                if alteration == 'replaced':
                    foreign.rename(pending)
                else:
                    os.link(foreign, pending)
        return data

    monkeypatch.setattr(recovery.csv_backup, '_read_regular', replace_marker_after_receipt_check)
    destination = tmp_path / 'replaced-marker'
    with pytest.raises(recovery.csv_backup.FileBackupError, match='Pending-Marker'):
        recovery.restore_backup(encrypted, destination, key)
    pending = destination / recovery.RESTORE_PENDING_FILE
    if alteration == 'missing':
        assert not pending.exists()
    else:
        assert pending.read_bytes() == replacement


def test_cli_receipts_contain_no_credentials_or_customer_content(source, tmp_path):
    root, _, _, session, *_ = source
    key = secrets.token_bytes(32)
    key_path = tmp_path / 'key.bin'
    key_file(key_path, key)
    encrypted = tmp_path / 'pair.enc'
    prefix = [sys.executable, '-X', 'utf8', '-m', 'tankai.web.recovery_backup']
    for action, input_path, destination in [('backup', root, encrypted), ('verify', encrypted, None),
                                          ('restore', encrypted, tmp_path / 'fresh')]:
        command = prefix + [action, '--source', str(input_path), '--key-file', str(key_path)]
        if destination:
            command += ['--destination', str(destination)]
        result = subprocess.run(command, cwd=Path(__file__).resolve().parents[1], capture_output=True,
                                text=True, encoding='utf-8', timeout=25)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)['jobs'] == 2
        for forbidden in (key.hex(), session.token, 'owner@example.test', 'Recovery-test-password-123', 'pending.csv'):
            assert forbidden not in result.stdout + result.stderr
