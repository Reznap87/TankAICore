# Coordinated private account and CSV recovery

The account/CSV recovery tool closes the CSV-only restore gap: an operator can
recover accounts, workspace identities and CSV jobs from one encrypted package,
then sign in with an existing account password and resume a saved draft. Both
databases are recovered into a fresh data root. The source databases are not
replaced, and a successful restore never changes the source account credentials.

This is a single-host private-pilot tool. It covers `auth.db` (schema v3) and
`csv-jobs.db` (schema v1) in one directory. It is not a whole-workspace or
production disaster-recovery claim. Chat history, vector/memory files, other
uploads, development jobs and the independent monetary ledger are outside the
package. Provider-call rate-limit records in the auth database are preserved;
they do not substitute for a monetary ledger.

## Operator commands

Run from the Core checkout after installing `requirements-backup.txt`. The key
file must hold exactly 32 random binary bytes and be protected separately from
backups, as described in [CSV file jobs](CSV_FILE_JOBS.md). Example paths below
are placeholders; the tool does not generate a deployment key or schedule a job.

```text
python -m tankai.web.recovery_backup backup --source /private/data --destination /protected/backups/account-csv.enc --key-file /protected/keys/recovery.key
python -m tankai.web.recovery_backup verify --source /protected/backups/account-csv.enc --key-file /protected/keys/recovery.key
python -m tankai.web.recovery_backup restore --source /protected/backups/account-csv.enc --destination /private/new-recovery-root --key-file /protected/keys/recovery.key
```

Source and destination parents must be trusted operator-owned directories. The
source must contain both databases. Inputs must be regular files, not symlinks;
their path and open-file identity, size and modification metadata must remain
stable through the complete bounded read. A concurrent rename, replacement or
write is rejected before any successful verification receipt. Missing databases
are not created. On POSIX, each source database must have exactly one directory
link, and its device, inode and link count must remain stable until its online
snapshot is complete. The read-only snapshot connection is opened while the
corresponding write reservation is held and is reused for the copy; a pathname
replacement after reservation cannot redirect the snapshot to an unreserved
database. This keeps pathname-bound WAL/SHM sidecars associated with the source
being copied. Windows hard-link policy remains an operator duty.
Existing output files/directories are never replaced. A restored
root is an offline recovery candidate, not an automatic
switch of a running service. Use `TANKAI_AUTH_DB` pointing to its `auth.db` and
`TANKAI_DATA_ROOT` pointing to the same new root for a private verification server.
The CSV UI additionally requires session auth and the loopback-only file flag.

## One consistent pair

The tool acquires SQLite `BEGIN IMMEDIATE` write reservations in the fixed order
auth then CSV, with a combined five-second acquisition bound. Once both are held,
separate read connections copy both stores using SQLite's online backup API.
Ordinary writers wait during the copy. Reservations are rolled back and closed
on both success and failure. The pair is then immutable in memory while schema,
integrity, quotas and completed-delivery checks run. Snapshot copying has one
shared 15-second deadline; snapshot size is bounded to 8 MiB auth plus 64 MiB CSV.

Committed WAL pages are included. The serialized snapshots use standalone
rollback-mode headers. Each CSV job must refer to an existing user and to the
correct workspace/tenant in the same account snapshot. Revoked memberships and
disabled users are preserved; no access is granted merely because an old CSV job
still exists. Unknown tables, views or triggers in the account store are rejected,
and the existing auth snapshot schema/version/foreign-key validator is reused.

## Encrypted format and validation

The fixed `TANKAI-ACCOUNT-CSV-RECOVERY\0\1` header is followed by a fresh 12-byte
nonce and AES-256-GCM ciphertext/tag. The authenticated context binds the format,
auth-v3 and CSV-v1 scopes. Inside the ciphertext, two fixed-width big-endian
lengths identify the auth snapshot and CSV snapshot. There are no user-provided
member names, archive paths or compression. Length mismatch, extra members and
CSV-only/foreign formats are rejected. Only ciphertext is persisted during
backup; the saved file remains bound to the exclusively created file identity
while its exact bytes are read back and validated before success is acknowledged.
A missing, replaced, hard-linked or modified output therefore cannot produce a
successful backup receipt.

`verify` authenticates the envelope and validates both databases and their
relationship without writing a recovered root. `restore` completes all validation
before creating the new root. Files start at POSIX mode 0600 and the root at 0700;
Windows requires appropriate operator-managed ACLs.

Every encrypted-package and key-file read uses the shared bounded stable-file
reader. On POSIX, those inputs must have exactly one directory link; hard-linked
aliases are rejected and link-count changes during a read fail closed. This keeps
the verified operator path bound to one file name. Windows hard-link and ACL
policy remains an operator/filesystem responsibility.

## Fresh login and failure handling

The restored copy retains password hashes, account IDs, roles, memberships,
deactivation flags, audit records and rate-limit history. All saved sessions and
agent tokens are invalidated **in the restored copy only**, including credentials
that were still active when the backup was made. A user must log in again with
their existing password. Agent credentials need a separately authorized reissue.
This avoids reactivating old credentials when recovering an earlier snapshot.

The restore creates `.tankai-recovery-pending` before writing either database.
It writes both databases and a value-free `recovery-receipt.json` as exclusive
new files. Each artifact must remain the same file identity and match its
expected bytes. Identity is checked immediately before and after each bounded
byte read, including the receipt read. After the receipt durability boundary,
both databases are checked again before the pending marker is removed; a
missing, replaced, hard-linked or modified database or receipt leaves the
recovered root blocked.
The marker must likewise remain the same exclusively created file, so a missing,
replaced or hard-linked marker blocks completion without deleting a foreign
file. On POSIX, the parent directory, completed database set, receipt and final
marker removal are explicit directory-fsync
durability boundaries before success is returned. The app refuses to start from
a root with this marker. A failed restore can leave a partial new directory;
inspect it and use a different new destination for a retry. Do not remove the
marker to force startup.
The app also refuses a completed recovery root if either member is missing, an
external `TANKAI_AUTH_DB` points elsewhere, or external auth-state URL/token
settings could overwrite the recovered accounts. Reconciling a remote auth-state
service is a separate recovery/deployment action.

Windows does not expose directory fsync through Python; directory-metadata
durability and ACL enforcement there remain an operator/filesystem responsibility.

The restored auth hash differs from the original snapshot hash because sessions
and agent credentials were invalidated. Receipts distinguish the original auth
snapshot hash from `restoredAuthSha256`. CSV bytes remain unchanged. Receipts
contain counts and hashes, not keys, tokens, email addresses or CSV contents.

## Evidence and remaining deployment work

Tests recover both databases into a fresh app root, authenticate through real
loopback HTTP with the original password, resume a saved draft and download the
original CSV/report. Other tests exercise old-session and agent-token denial,
disabled accounts, quota history, both write barriers, lock-failure cleanup,
cross-database identity mismatch, tampering, malformed encrypted members, existing
destinations, incomplete restore and stale external-account configuration.

```text
python -m pytest -q tests/test_recovery_backup.py tests/test_auth_state.py tests/test_file_backup.py
```

Tests use synthetic accounts/data and ephemeral test keys, with no provider calls.
Production key custody, automatic/offsite copies, monetary-ledger recovery and
reconciliation with Cloudflare account snapshots still need separate proof and
activation. The running databases remain in their existing plaintext format.

References: [SQLite transactions](https://www.sqlite.org/lang_transaction.html),
[online backup](https://www.sqlite.org/backup.html), and
[authenticated encryption](https://cryptography.io/en/stable/hazmat/primitives/aead/).
