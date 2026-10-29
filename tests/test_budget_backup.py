"""Real authenticated encryption and SQLite, synthetic keys/data, no network."""
import builtins
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest

from tankai.core import budget_backup as backup
from tankai.core import monetary_budget as monetary
from tankai.core.monetary_budget import BudgetDenied, MonetaryBudget
from test_budget_snapshots import budget, no_network
from test_monetary_budget import Clock, OWNER, begin, reserve


@pytest.fixture
def saved(tmp_path, budget):
    begin(budget)
    reserve(budget)
    key = secrets.token_bytes(32)
    path = tmp_path/"private.enc"
    before = budget.snapshot()
    receipt = backup.create_backup(budget, path, key)
    assert budget.snapshot() == before
    return path, key, receipt, before


def options(budget, receipt):
    return dict(expected_sha256=receipt["encrypted_sha256"], expected_policy=budget.policy)


def test_ciphertext_verified_and_recovery_after_original_loss(tmp_path, budget, saved):
    path, key, receipt, before = saved
    encrypted = path.read_bytes()
    assert encrypted.startswith(backup.MAGIC)
    assert b"SQLite format 3" not in encrypted and OWNER.encode() not in encrypted
    assert receipt["persisted_ciphertext_verified"]
    assert OWNER not in str(receipt) and str(path) not in str(receipt) and key.hex() not in str(receipt)
    approved_policy = budget.policy
    budget.close()
    budget.path.unlink()  # Synthetic fixture, closed; ciphertext is all that remains.
    assert backup.verify_backup(path, key, expected_sha256=receipt["encrypted_sha256"],
                                expected_policy=approved_policy)["ok"]
    target = tmp_path/"isolated-restored.sqlite"
    result = backup.restore_backup(path, target, key, expected_sha256=receipt["encrypted_sha256"],
                                    expected_policy=approved_policy, clock=Clock())
    assert result["restored_state_verified"]
    with MonetaryBudget(target, clock=Clock()) as recovered:
        assert recovered.snapshot() == before
        assert not reserve(recovered).dispatch_allowed
        with pytest.raises(BudgetDenied, match="concurrent_job_limit"): begin(recovered, "next")


def test_each_backup_has_fresh_nonce_and_ciphertext(tmp_path, budget, saved):
    path, key, first, _ = saved
    second_path = tmp_path/"second.enc"
    second = backup.create_backup(budget, second_path, key)
    assert path.read_bytes()[len(backup.MAGIC):len(backup.MAGIC)+12] != second_path.read_bytes()[len(backup.MAGIC):len(backup.MAGIC)+12]
    assert first["encrypted_sha256"] != second["encrypted_sha256"]
    assert first["snapshot_sha256"] == second["snapshot_sha256"]


@pytest.mark.parametrize("key", [None, "x"*32, b"", b"x"*16, b"x"*24, b"x"*33, bytearray(b"x"*32)])
def test_key_must_be_exact_immutable_256_bit_binary_value(tmp_path, budget, key):
    target = tmp_path/"invalid-key.enc"
    with pytest.raises(backup.BudgetBackupError): backup.create_backup(budget, target, key)
    assert not target.exists()


def test_wrong_key_authentication_blocks_before_destination(tmp_path, budget, saved):
    path, _, receipt, _ = saved
    with pytest.raises(backup.BudgetBackupError):
        backup.restore_backup(path, tmp_path/"new.sqlite", secrets.token_bytes(32), **options(budget,receipt))
    assert not (tmp_path/"new.sqlite").exists()


@pytest.mark.parametrize("field,change", [("owner_id","another-owner"), ("max_attempts_per_job",4),
                                         ("total_micro_eur",9_000_000)])
def test_trusted_policy_context_is_authenticated(tmp_path, budget, saved, field, change):
    path, key, receipt, _ = saved
    with pytest.raises(backup.BudgetBackupError):
        backup.restore_backup(path, tmp_path/"new.sqlite", key,
            expected_sha256=receipt["encrypted_sha256"], expected_policy=replace(budget.policy, **{field:change}))
    assert not (tmp_path/"new.sqlite").exists()


@pytest.mark.parametrize("position", [0, len(backup.MAGIC), len(backup.MAGIC)+12, -1])
def test_tampering_fails_even_when_attacker_recomputes_file_digest(tmp_path, budget, saved, position):
    path, key, _, _ = saved
    changed = bytearray(path.read_bytes())
    changed[position] ^= 1
    path.write_bytes(changed)
    with pytest.raises(backup.BudgetBackupError):
        backup.restore_backup(path, tmp_path/"new.sqlite", key,
            expected_sha256=hashlib.sha256(changed).hexdigest(), expected_policy=budget.policy)
    assert not (tmp_path/"new.sqlite").exists()


@pytest.mark.parametrize("pin", [None, "", "A"*64, "0"*64, True])
def test_missing_invalid_or_wrong_independent_pin_is_rejected(budget, saved, pin):
    path, key, _, _ = saved
    with pytest.raises(backup.BudgetBackupError):
        backup.verify_backup(path, key, expected_sha256=pin, expected_policy=budget.policy)


def test_invalid_pin_does_not_open_source(budget, monkeypatch):
    monkeypatch.setattr(backup, "_read_regular", lambda *a:pytest.fail("invalid pin must not open a file"))
    with pytest.raises(backup.BudgetBackupError):
        backup.verify_backup(Path("absent"), secrets.token_bytes(32), expected_sha256=None,
                              expected_policy=budget.policy)


@pytest.mark.parametrize("kind", ["truncate", "append", "foreign-format", "empty"])
def test_malformed_envelopes_have_no_restore_side_effect(tmp_path, budget, saved, kind):
    path, key, _, _ = saved
    data = path.read_bytes()
    data = {"truncate":data[:-5], "append":data+b"extra", "foreign-format":b"FOREIGN"+data,
            "empty":b""}[kind]
    path.write_bytes(data)
    with pytest.raises(backup.BudgetBackupError):
        backup.restore_backup(path, tmp_path/"new.sqlite", key,
            expected_sha256=hashlib.sha256(data).hexdigest(), expected_policy=budget.policy)
    assert not (tmp_path/"new.sqlite").exists()


def test_valid_encryption_cannot_hide_invalid_database(tmp_path, budget):
    key, nonce = secrets.token_bytes(32), secrets.token_bytes(12)
    data = b"SQLite format 3\x00"+b"invalid"*100
    encrypted = backup.MAGIC+nonce+backup._cipher(key).encrypt(nonce,data,backup.AAD+backup._policy_context(budget.policy))
    source = tmp_path/"invalid.enc"
    source.write_bytes(encrypted)
    with pytest.raises(backup.BudgetBackupError):
        backup.restore_backup(source, tmp_path/"new.sqlite", key,
            expected_sha256=hashlib.sha256(encrypted).hexdigest(), expected_policy=budget.policy)
    assert not (tmp_path/"new.sqlite").exists()


def test_existing_backup_and_existing_restore_or_sidecars_are_preserved(tmp_path, budget, saved):
    path, key, receipt, _ = saved
    before = path.read_bytes()
    with pytest.raises(backup.BudgetBackupError): backup.create_backup(budget, path, key)
    assert path.read_bytes() == before
    target = tmp_path/"new.sqlite"
    for suffix in ("", "-wal", "-shm", "-journal"):
        existing = Path(str(target)+suffix)
        existing.write_bytes(b"operator data")
        with pytest.raises(backup.BudgetBackupError):
            backup.restore_backup(path, target, key, **options(budget,receipt))
        assert existing.read_bytes() == b"operator data"
        existing.unlink()


def test_failed_ciphertext_sync_cannot_be_acknowledged_or_overwritten(tmp_path, budget, monkeypatch):
    target = tmp_path/"failed.enc"
    key = secrets.token_bytes(32)
    with monkeypatch.context() as patch:
        patch.setattr(os,"fsync",lambda *a:(_ for _ in ()).throw(OSError("synthetic write failure")))
        with pytest.raises(backup.BudgetBackupError): backup.create_backup(budget,target,key)
    before = target.read_bytes()
    with pytest.raises(backup.BudgetBackupError): backup.create_backup(budget,target,key)
    assert target.read_bytes() == before


@pytest.mark.skipif(os.name == "nt", reason="Windows does not expose directory fsync through Python")
@pytest.mark.parametrize("operation", ["backup", "restore"])
def test_success_receipts_require_durable_directory_entries(
    tmp_path, budget, monkeypatch, operation,
):
    key = secrets.token_bytes(32)
    encrypted = tmp_path / "durable.enc"
    receipt = backup.create_backup(budget, encrypted, key)
    observed = []
    real_fsync = backup.os.fsync

    def recording_fsync(descriptor):
        observed.append(stat.S_ISDIR(os.fstat(descriptor).st_mode))
        return real_fsync(descriptor)

    monkeypatch.setattr(backup.os, "fsync", recording_fsync)
    if operation == "backup":
        backup.create_backup(budget, tmp_path / "second.enc", key)
    else:
        backup.restore_backup(
            encrypted, tmp_path / "restored.sqlite", key, **options(budget, receipt)
        )
    assert True in observed


def test_identical_replacement_of_created_ciphertext_is_rejected(tmp_path, budget, monkeypatch):
    target = tmp_path / "replaceable.enc"
    created = tmp_path / "exclusively-created.enc"
    real_sync = backup._sync_directory
    replaced = False

    def replace_after_sync(parent):
        nonlocal replaced
        real_sync(parent)
        if not replaced and target.exists():
            replaced = True
            target.rename(created)
            target.write_bytes(created.read_bytes())

    monkeypatch.setattr(backup, "_sync_directory", replace_after_sync)
    with pytest.raises(backup.BudgetBackupError, match="persisted encrypted backup was replaced"):
        backup.create_backup(budget, target, secrets.token_bytes(32))
    assert target.read_bytes() == created.read_bytes()


def test_in_place_mutated_restored_database_cannot_receive_success(
    tmp_path, budget, saved, monkeypatch,
):
    path, key, receipt, _ = saved
    begin(budget, "later-job")
    reserve(budget, "later-attempt", job_id="later-job")
    altered_snapshot = budget.export_snapshot()
    target = tmp_path / "mutated-restore.sqlite"
    real_sync = monetary._sync_directory
    mutated = False

    def mutate_after_directory_sync(parent):
        nonlocal mutated
        real_sync(parent)
        if not mutated and target.exists():
            mutated = True
            with target.open("r+b") as output:
                output.write(altered_snapshot)
                output.truncate()
                output.flush()
                os.fsync(output.fileno())

    monkeypatch.setattr(monetary, "_sync_directory", mutate_after_directory_sync)
    with pytest.raises(backup.BudgetBackupError, match="restore failed closed"):
        backup.restore_backup(target=target, source=path, key=key, **options(budget, receipt))
    assert target.read_bytes() == altered_snapshot


def test_persisted_ciphertext_is_checked_after_writing(tmp_path, budget, monkeypatch):
    original = backup._read_regular
    def tamper(path):
        data = bytearray(original(path)); data[-1] ^= 1
        return bytes(data)
    monkeypatch.setattr(backup,"_read_regular",tamper)
    with pytest.raises(backup.BudgetBackupError):
        backup.create_backup(budget,tmp_path/"tampered.enc",secrets.token_bytes(32))


def test_oversized_file_rejected_before_reading(budget, tmp_path):
    path = tmp_path/"too-big.enc"
    with path.open("wb") as handle: handle.truncate(backup.MAX_ENCRYPTED_BYTES+1)
    with pytest.raises(backup.BudgetBackupError):
        backup.verify_backup(path,secrets.token_bytes(32),expected_sha256="0"*64,expected_policy=budget.policy)


def test_metadata_change_during_read_is_rejected(budget, saved, monkeypatch):
    path,key,receipt,_ = saved
    original, calls = os.fstat, []
    def changed(fd):
        info = original(fd)
        calls.append(fd)
        if len(calls) == 2:
            attrs = {name:getattr(info,name) for name in
                     ("st_dev","st_ino","st_mode","st_nlink","st_size","st_mtime_ns","st_ctime_ns")}
            return SimpleNamespace(**{**attrs,"st_mtime_ns":info.st_mtime_ns+1})
        return info
    monkeypatch.setattr(os,"fstat",changed)
    with pytest.raises(backup.BudgetBackupError,match="changed"):
        backup.verify_backup(path,key,**options(budget,receipt))


def test_linked_ciphertext_is_rejected(budget, saved, tmp_path):
    path,key,receipt,_ = saved
    link = tmp_path/"linked.enc"
    try: link.symlink_to(path)
    except (OSError,NotImplementedError): pytest.skip("symlink privilege unavailable")
    with pytest.raises(backup.BudgetBackupError): backup.verify_backup(link,key,**options(budget,receipt))


@pytest.mark.skipif(os.name == "nt", reason="POSIX hard-link contract")
def test_hardlinked_ciphertext_is_rejected(budget, saved, tmp_path):
    path,key,receipt,_ = saved
    alias = tmp_path/"alias.enc"
    try:
        os.link(path, alias)
    except OSError:
        pytest.skip("hard links are unavailable")
    with pytest.raises(backup.BudgetBackupError, match="exclusively named"):
        backup.verify_backup(path,key,**options(budget,receipt))


def test_fifo_is_rejected_without_opening(budget, tmp_path, monkeypatch):
    if not hasattr(os,"mkfifo"): pytest.skip("FIFO requires POSIX")
    path = tmp_path/"pipe"
    os.mkfifo(path)
    monkeypatch.setattr(os,"open",lambda *a,**k:pytest.fail("nonregular input must not be opened"))
    with pytest.raises(backup.BudgetBackupError):
        backup.verify_backup(path,secrets.token_bytes(32),expected_sha256="0"*64,expected_policy=budget.policy)


def test_optional_crypto_missing_blocks_backup_before_writing(tmp_path, budget, monkeypatch):
    original = builtins.__import__
    def without_crypto(name,*args,**kwargs):
        if name.startswith("cryptography"): raise ImportError("synthetic unavailable dependency")
        return original(name,*args,**kwargs)
    monkeypatch.setattr(builtins,"__import__",without_crypto)
    with pytest.raises(backup.BudgetBackupError,match="requirements-backup"):
        backup.create_backup(budget,tmp_path/"new.enc",secrets.token_bytes(32))
    assert not (tmp_path/"new.enc").exists()


def test_web_import_does_not_activate_optional_encryption():
    code = '''
import sys
class RejectCrypto:
    def find_spec(self, name, *args):
        if name.startswith("cryptography"): raise AssertionError("web startup imports optional backup crypto")
sys.meta_path.insert(0, RejectCrypto())
import tankai.web.server
assert "tankai.core.budget_backup" not in sys.modules
print("OPTIONAL_CRYPTO_NOT_ACTIVATED")
'''
    result = subprocess.run([sys.executable,"-X","utf8","-c",code],capture_output=True,text=True,timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "OPTIONAL_CRYPTO_NOT_ACTIVATED"
