"""Real encrypted files, adversarial input and a separate restored CSV store."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import subprocess
import sys

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import pytest

from tankai.web import file_backup as backup
from tankai.web.file_jobs import FileJobStore
from test_file_jobs import CASES, actor, opts, upload


@pytest.fixture
def original(tmp_path):
    source = tmp_path / 'source.db'
    store = FileJobStore(source)
    identity = actor()
    draft = store.create(identity, upload(name='saved-choice.csv'))
    draft = store.update(identity, draft['id'], {'revision': 1, 'options': opts(trim=True)})
    completed = store.create(identity, upload(name='completed.csv'))
    store.update(identity, completed['id'], {'revision': 1}, complete=True)
    return store, source, identity, draft, completed


def key_file(path, key):
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, 'wb') as output:
        output.write(key)


def test_saved_ciphertext_and_exact_draft_and_delivery_roundtrip(original, tmp_path):
    store, source, identity, draft, completed = original
    key = AESGCM.generate_key(bit_length=256)
    encrypted = tmp_path / 'backup.enc'
    receipt = backup.create_backup(source, encrypted, key)
    saved = encrypted.read_bytes()
    assert receipt['persistedCiphertextVerified'] and receipt['cipher'] == 'AES-256-GCM'
    assert receipt['jobs'] == 2 and receipt['draftJobs'] == receipt['completedJobs'] == 1
    assert receipt['encryptedSha256'] == hashlib.sha256(saved).hexdigest()
    # Independent AEAD decode of the saved envelope, without the backup decrypt helper.
    payload = AESGCM(key).decrypt(saved[len(backup.MAGIC):len(backup.MAGIC) + 12],
                                  saved[len(backup.MAGIC) + 12:], backup.AAD)
    assert payload.startswith(b'SQLite format 3\0')
    assert receipt['snapshotSha256'] == hashlib.sha256(payload).hexdigest()
    assert b'saved-choice.csv' not in saved
    assert backup.verify_backup(encrypted, key)['jobs'] == 2
    destination = tmp_path / 'fresh'
    restored_receipt = backup.restore_backup(encrypted, destination, key)
    assert restored_receipt['snapshotSha256'] == receipt['snapshotSha256']
    restored = FileJobStore(destination / 'csv-jobs.db')
    assert restored.get(identity, draft['id']) == store.get(identity, draft['id'])
    for kind in ('csv', 'report'):
        assert restored.download(identity, completed['id'], kind) == store.download(identity, completed['id'], kind)
    restored.update(identity, draft['id'], {'revision': draft['revision']}, complete=True)
    assert not store.get(identity, draft['id'])['completed']
    assert restored.list(actor()) == []
    if os.name != 'nt':
        assert encrypted.stat().st_mode & 0o777 == 0o600
        assert destination.stat().st_mode & 0o777 == 0o700
        assert (destination / 'csv-jobs.db').stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('name,source,options,expected,counts', CASES, ids=[case[0] for case in CASES])
def test_ten_saved_deliveries_survive_encrypted_restore(tmp_path, name, source, options, expected, counts):
    store = FileJobStore(tmp_path / 'original.db')
    identity = actor()
    job = store.create(identity, upload(source=source))
    job = store.update(identity, job['id'], {'revision': 1, 'options': options})
    store.update(identity, job['id'], {'revision': job['revision']}, complete=True)
    key = secrets.token_bytes(32)
    encrypted = tmp_path / 'backup.enc'
    backup.create_backup(store.path, encrypted, key)
    backup.restore_backup(encrypted, tmp_path / 'restored', key)
    restored = FileJobStore(tmp_path / 'restored/csv-jobs.db')
    assert restored.download(identity, job['id'], 'csv')[0] == ('\ufeff' + expected).encode()
    assert restored.download(identity, job['id'], 'report') == store.download(identity, job['id'], 'report')


@pytest.mark.parametrize('modification', ['magic', 'version', 'nonce', 'payload', 'tag', 'truncate', 'append', 'wrong-key'])
def test_wrong_key_or_tampering_is_rejected_before_restore_writes(original, tmp_path, modification):
    _, source, *_ = original
    key = secrets.token_bytes(32)
    encrypted = tmp_path / 'backup.enc'
    backup.create_backup(source, encrypted, key)
    content = bytearray(encrypted.read_bytes())
    positions = {'magic': 0, 'version': len(backup.MAGIC) - 1, 'nonce': len(backup.MAGIC),
                 'payload': len(backup.MAGIC) + 30, 'tag': len(content) - 1}
    if modification in positions:
        content[positions[modification]] ^= 1
    elif modification == 'truncate':
        content = content[:-1]
    elif modification == 'append':
        content += b'extra'
    else:
        key = secrets.token_bytes(32)
    encrypted.write_bytes(content)
    with pytest.raises(backup.FileBackupError):
        backup.restore_backup(encrypted, tmp_path / 'must-not-exist', key)
    assert not (tmp_path / 'must-not-exist').exists()


def test_no_overwrites_or_existing_directory_sidecars(original, tmp_path):
    _, source, *_ = original
    key = secrets.token_bytes(32)
    encrypted = tmp_path / 'backup.enc'
    backup.create_backup(source, encrypted, key)
    saved = encrypted.read_bytes()
    with pytest.raises(FileExistsError):
        backup.create_backup(source, encrypted, key)
    assert encrypted.read_bytes() == saved
    target = tmp_path / 'existing'
    target.mkdir()
    (target / 'csv-jobs.db').write_bytes(b'keep-database')
    (target / 'csv-jobs.db-wal').write_bytes(b'keep-wal')
    with pytest.raises(FileExistsError):
        backup.restore_backup(encrypted, target, key)
    assert (target / 'csv-jobs.db').read_bytes() == b'keep-database'
    assert (target / 'csv-jobs.db-wal').read_bytes() == b'keep-wal'


def test_snapshots_include_committed_wal_pages(original, tmp_path):
    store, source, identity, draft, _ = original
    db = sqlite3.connect(source)
    try:
        assert db.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
        db.execute('PRAGMA wal_autocheckpoint=0')
        db.execute('UPDATE csv_jobs SET goal=? WHERE id=?', ('committed WAL choice', draft['id']))
        db.commit()
        db.execute('UPDATE csv_jobs SET goal=? WHERE id=?', ('uncommitted must not leak', draft['id']))
        key = secrets.token_bytes(32)
        encrypted = tmp_path / 'wal.enc'
        backup.create_backup(source, encrypted, key)
        backup.restore_backup(encrypted, tmp_path / 'restored', key)
        restored = FileJobStore(tmp_path / 'restored/csv-jobs.db')
        assert restored.get(identity, draft['id'])['goal'] == 'committed WAL choice'
    finally:
        db.rollback()
        db.close()


@pytest.mark.parametrize('corruption', ['unknown-table', 'trigger', 'wrong-csv', 'wrong-report',
                                     'missing-report', 'bad-options', 'bad-owner', 'extra-unique-missing'])
def test_invalid_database_is_rejected_before_encrypted_output(original, tmp_path, corruption):
    _, source, _, _, completed = original
    db = sqlite3.connect(source)
    try:
        if corruption == 'unknown-table':
            db.execute('CREATE TABLE unwanted(secret TEXT)')
        elif corruption == 'trigger':
            db.execute('CREATE TRIGGER sqliteX_before BEFORE INSERT ON csv_jobs BEGIN DELETE FROM csv_jobs; END')
        elif corruption == 'wrong-csv':
            db.execute('UPDATE csv_jobs SET csv=? WHERE id=?', (b'changed', completed['id']))
        elif corruption == 'wrong-report':
            db.execute('UPDATE csv_jobs SET report=? WHERE id=?', ('{}', completed['id']))
        elif corruption == 'missing-report':
            db.execute('UPDATE csv_jobs SET report=NULL WHERE id=?', (completed['id'],))
        elif corruption == 'bad-options':
            db.execute('UPDATE csv_jobs SET options=?', ('{"trim": "false"}',))
        elif corruption == 'bad-owner':
            db.execute('UPDATE csv_jobs SET owner=?', ('not-a-uuid',))
        else:
            db.execute('CREATE TABLE replacement AS SELECT * FROM csv_jobs')
            db.execute('DROP TABLE csv_jobs')
            db.execute('ALTER TABLE replacement RENAME TO csv_jobs')
        db.commit()
    finally:
        db.close()
    encrypted = tmp_path / 'must-not-exist.enc'
    with pytest.raises(backup.FileBackupError):
        backup.create_backup(source, encrypted, secrets.token_bytes(32))
    assert not encrypted.exists()


def test_repeated_backups_have_distinct_nonces(original, tmp_path):
    _, source, *_ = original
    key = secrets.token_bytes(32)
    first, second = tmp_path / 'first.enc', tmp_path / 'second.enc'
    backup.create_backup(source, first, key)
    backup.create_backup(source, second, key)
    assert first.read_bytes()[len(backup.MAGIC):len(backup.MAGIC) + 12] != second.read_bytes()[len(backup.MAGIC):len(backup.MAGIC) + 12]
    assert backup.verify_backup(first, key)['snapshotSha256'] == backup.verify_backup(second, key)['snapshotSha256']


def test_old_indented_schema_is_compatible_and_snapshot_timeout_is_bounded(original, tmp_path, monkeypatch):
    _, source, *_ = original
    # Schema formatting from the previous CSV commit must not invalidate existing data.
    legacy = sqlite3.connect(tmp_path / 'legacy.db')
    try:
        legacy.execute(backup.CSV_SCHEMA.replace('\n', '\n        '))
        legacy.commit()
    finally:
        legacy.close()
    backup.create_backup(tmp_path / 'legacy.db', tmp_path / 'legacy.enc', secrets.token_bytes(32))
    monkeypatch.setattr(backup, 'SNAPSHOT_TIMEOUT_SECONDS', -1)
    with pytest.raises(backup.FileBackupError, match='Zeit- oder Speichergrenze'):
        backup.create_backup(source, tmp_path / 'timed-out.enc', secrets.token_bytes(32))
    assert not (tmp_path / 'timed-out.enc').exists()


def test_authenticated_but_wrong_database_is_rejected_before_target_creation(tmp_path):
    db = sqlite3.connect(':memory:')
    try:
        db.execute('CREATE TABLE unrelated(value)')
        payload = db.serialize()
    finally:
        db.close()
    key, nonce = secrets.token_bytes(32), secrets.token_bytes(12)
    source = tmp_path / 'wrong-db.enc'
    source.write_bytes(backup.MAGIC + nonce + AESGCM(key).encrypt(nonce, payload, backup.AAD))
    with pytest.raises(backup.FileBackupError):
        backup.restore_backup(source, tmp_path / 'untouched', key)
    assert not (tmp_path / 'untouched').exists()


def test_linked_inputs_or_outputs_are_not_followed(original, tmp_path):
    _, source, *_ = original
    linked_source = tmp_path / 'source-link.db'
    try:
        linked_source.symlink_to(source)
    except OSError:
        pytest.skip('Creating symlinks requires a privilege on this Windows host')
    key = secrets.token_bytes(32)
    with pytest.raises(backup.FileBackupError):
        backup.create_backup(linked_source, tmp_path / 'must-not-exist.enc', key)
    target = tmp_path / 'keep.txt'
    target.write_bytes(b'leave unchanged')
    linked_output = tmp_path / 'output-link.enc'
    linked_output.symlink_to(target)
    with pytest.raises(FileExistsError):
        backup.create_backup(source, linked_output, key)
    assert target.read_bytes() == b'leave unchanged'


def test_backup_path_replacement_during_read_is_rejected(original, tmp_path, monkeypatch):
    _, source, *_ = original
    key = secrets.token_bytes(32)
    encrypted = tmp_path / 'replaceable.enc'
    moved = tmp_path / 'opened-inode.enc'
    backup.create_backup(source, encrypted, key)
    original_fstat = backup.os.fstat
    replaced = False

    def replace_after_open(descriptor):
        nonlocal replaced
        info = original_fstat(descriptor)
        if not replaced:
            replaced = True
            encrypted.rename(moved)
            encrypted.write_bytes(b'x' * info.st_size)
        return info

    monkeypatch.setattr(backup.os, 'fstat', replace_after_open)
    with pytest.raises(backup.FileBackupError, match='geändert'):
        backup.verify_backup(encrypted, key)
    assert encrypted.read_bytes() != moved.read_bytes()


@pytest.mark.skipif(os.name == 'nt', reason='POSIX hard-link contract')
def test_hard_linked_backup_and_key_inputs_are_rejected(original, tmp_path):
    _, source, *_ = original
    key = secrets.token_bytes(32)
    encrypted = tmp_path / 'backup.enc'
    backup.create_backup(source, encrypted, key)
    encrypted_alias = tmp_path / 'backup-alias.enc'
    key_path = tmp_path / 'recovery.key'
    key_alias = tmp_path / 'recovery-alias.key'
    key_file(key_path, key)
    try:
        os.link(encrypted, encrypted_alias)
        os.link(key_path, key_alias)
    except OSError:
        pytest.skip('Creating hard links is unavailable on this filesystem')

    with pytest.raises(backup.FileBackupError, match='Hardlink'):
        backup.verify_backup(encrypted, key)
    with pytest.raises(backup.FileBackupError, match='Hardlink'):
        backup._read_regular(key_path, 32, key_file=True)


@pytest.mark.skipif(os.name == 'nt', reason='POSIX permission contract; Windows uses directory ACLs')
def test_cli_rejects_key_files_readable_by_other_accounts(original, tmp_path):
    _, source, *_ = original
    key = tmp_path / 'wide.key'
    key_file(key, secrets.token_bytes(32))
    key.chmod(0o644)
    with pytest.raises(backup.FileBackupError, match='nur dem Besitzer'):
        backup._read_regular(key, 32, key_file=True)


def test_bounds_and_missing_source_never_create_a_source_or_restore_directory(tmp_path, monkeypatch):
    missing = tmp_path / 'missing.db'
    with pytest.raises(backup.FileBackupError):
        backup.create_backup(missing, tmp_path / 'output.enc', secrets.token_bytes(32))
    assert not missing.exists()
    huge = tmp_path / 'oversized.enc'
    huge.write_bytes(b'x' * 100)
    monkeypatch.setattr(backup, 'MAX_ENCRYPTED_BYTES', 99)
    with pytest.raises(backup.FileBackupError):
        backup.restore_backup(huge, tmp_path / 'fresh', secrets.token_bytes(32))
    assert not (tmp_path / 'fresh').exists()


def test_interrupted_write_does_not_remove_a_preexisting_file(original, tmp_path, monkeypatch):
    _, source, *_ = original
    def fail(_):
        raise OSError('synthetic fsync failure')
    monkeypatch.setattr(backup.os, 'fsync', fail)
    output = tmp_path / 'failed.enc'
    with pytest.raises(OSError):
        backup.create_backup(source, output, secrets.token_bytes(32))
    assert not output.exists()
    assert source.is_file()


@pytest.mark.skipif(os.name == 'nt', reason='Windows does not expose directory fsync through Python')
def test_success_receipts_require_directory_metadata_flushes(original, tmp_path, monkeypatch):
    """A successful backup/restore must persist new directory entries, not only file bytes."""
    _, source, *_ = original
    observed = []
    real_fsync = backup.os.fsync

    def recording_fsync(descriptor):
        observed.append(stat.S_ISDIR(os.fstat(descriptor).st_mode))
        return real_fsync(descriptor)

    monkeypatch.setattr(backup.os, 'fsync', recording_fsync)
    encrypted = tmp_path / 'durable.enc'
    backup.create_backup(source, encrypted, secrets.token_bytes(32))
    assert True in observed

    observed.clear()
    key = secrets.token_bytes(32)
    second = tmp_path / 'restore.enc'
    backup.create_backup(source, second, key)
    observed.clear()
    backup.restore_backup(second, tmp_path / 'durable-restore', key)
    assert observed.count(True) >= 2


@pytest.mark.parametrize('key', [b'', b'x' * 16, b'x' * 31, b'x' * 33, 'not-binary'])
def test_only_256_bit_binary_keys_are_accepted(original, tmp_path, key):
    _, source, *_ = original
    with pytest.raises(backup.FileBackupError):
        backup.create_backup(source, tmp_path / 'output.enc', key)
    assert not (tmp_path / 'output.enc').exists()


def test_cli_emits_receipts_without_rows_or_keys(original, tmp_path):
    _, source, *_ = original
    key = secrets.token_bytes(32)
    key_path = tmp_path / 'key.bin'
    key_file(key_path, key)
    encrypted = tmp_path / 'backup.enc'
    command = [sys.executable, '-X', 'utf8', '-m', 'tankai.web.file_backup']
    for action, input_path, output_path in [('backup', source, encrypted), ('verify', encrypted, None),
                                           ('restore', encrypted, tmp_path / 'restored')]:
        args = [*command, action, '--source', str(input_path), '--key-file', str(key_path)]
        if output_path:
            args += ['--destination', str(output_path)]
        process = subprocess.run(args, capture_output=True, text=True, encoding='utf-8', timeout=20,
                                 cwd=Path(__file__).resolve().parents[1])
        assert process.returncode == 0, process.stderr
        assert json.loads(process.stdout)['jobs'] == 2
        assert key.hex() not in process.stdout + process.stderr
        assert 'saved-choice.csv' not in process.stdout + process.stderr
    key_path.write_bytes(secrets.token_bytes(32))
    process = subprocess.run([*command, 'verify', '--source', str(encrypted), '--key-file', str(key_path)],
                             capture_output=True, text=True, encoding='utf-8', timeout=20,
                             cwd=Path(__file__).resolve().parents[1])
    assert process.returncode == 1 and not process.stdout
    assert not json.loads(process.stderr)['ok']
