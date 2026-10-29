# Encrypted operator backup for the monetary ledger

`tankai.core.budget_backup` adds authenticated encryption to the bounded ledger
snapshot and new-destination restore primitives. It is optional operator code,
not an HTTP route, server-startup feature, scheduler or production cutover.
No deployment key, account permission, provider or live configuration is created.

This covers **one monetary ledger only**. PR #102 separately covers account/CSV
snapshots using the same AES-256-GCM primitive and optional dependency. The two
formats have different authenticated domains and are deliberately not accepted
as each other. Independently captured files are not a coordinated recovery set.
Workspace memory, history, vectors and source grants are not added to this file.

## Caller contract

Operator environments install `requirements-backup.txt`. The main server does
not import this module or require this dependency to start. Development tests
install the same optional dependency and use ephemeral synthetic keys.

- `create_backup(ledger, destination, key)` exports a validated consistent ledger
  snapshot, encrypts it, writes a new ciphertext file and reads/authenticates that
  saved file before acknowledging success. It returns only format/count/hash
  metadata and a `persisted_ciphertext_verified` flag.
- `verify_backup(source, key, expected_sha256=..., expected_policy=...)`
  authenticates the file and runs strict ledger validation without creating a
  recovered destination. Validation uses private temporary SQLite copies.
- `restore_backup(source, destination, key, expected_sha256=...,
  expected_policy=..., clock=...)` completes authentication and policy/state
  validation before the existing `restore_new` primitive may create a new ledger.
  It preserves holds, stops, quotas and replay protection and closes the new
  connection before returning. It neither reissues spending authority nor
  decides whether upstream work has stopped.

The key must be exactly 32 immutable binary bytes from an independently protected
operator key source, not a password, root credential, browser field or repository
secret discovered automatically by this module. No key file is generated or
read here. Production key custody, rotation, access controls and recovery of a
lost key remain operator/deployment requirements. Receipts do not include the
key, owner ID, path, billing contents or raw snapshot bytes.

The encrypted-file SHA-256 pin and complete `PilotPolicy` must be independently
trusted. Invalid pins fail before file opening; a valid-looking wrong pin still
fails. A pin read from the untrusted backup itself is not a freshness witness.

## Envelope and resource bounds

The fixed `TANKAI-MONETARY-BACKUP\0\1` header is followed by a newly sampled
12-byte nonce and AES-256-GCM ciphertext with its full 16-byte authentication tag.
The authenticated context contains the format/algorithm/ledger-policy version
and the SHA-256 of canonical independently approved policy fields. Changing the
owner, limits or policy lifetime invalidates authentication, before SQLite
restoration. No fallback cipher, passphrase KDF, archive member names, compression
or external fetch exists.

Snapshot plaintext is bounded to 8 MiB; the complete ciphertext has a fixed
corresponding maximum. AES-GCM is provided by `cryptography`, not implemented in
application code. Nonces are sampled with `secrets.token_bytes(12)` and must never
be reused with a key. See the [library's authenticated-encryption contract](https://cryptography.io/en/stable/hazmat/primitives/aead/).

Only regular bounded files are read, using no-follow/nonblocking flags where
available, with file identity/size/mtime checks before and after reading.
POSIX additionally requires exactly one directory link and checks ctime; a
hard-linked alias is rejected and link-count changes remain part of the stable
read identity. Windows descriptor/path ctime can have different legacy meanings,
so it is not used there; file ID, link count, size, mtime, trusted digest and
authentication tag still apply, while hard-link/ACL policy remains an operator
filesystem responsibility. Missing, linked, nonregular, changed, oversized,
truncated, appended or foreign-format inputs fail closed.

Output creation is exclusive. The persisted ciphertext remains bound to the
identity returned by that exclusive creation before and after its bounded,
authenticated readback. Restored ledger files are likewise read back through a
descriptor bound to their exclusive creation identity and must match the exact
decrypted snapshot bytes before initialization can succeed. A missing, replaced,
hard-linked or modified output cannot receive a successful persistence or restore
receipt, even when it remains a valid ledger under the same policy. Existing
backups, restored databases and SQLite sidecars are never
replaced. A write/sync/verification failure may leave a new diagnostic ciphertext
file; it is not acknowledged as a backup, and retry cannot overwrite it. On
POSIX, successful backup and restore receipts additionally
require the new directory entry to be synced. Source/destination parents must be
trusted operator directories. New files request POSIX mode 0600; Windows ACLs
and directory-metadata durability remain operator-managed.

## Exact security boundary

The permanent backup artifact is encrypted. Runtime databases retain their
existing plaintext format, and the underlying snapshot validator uses private
temporary plaintext SQLite files. This is not encryption of all runtime data,
secure erasure, memory zeroization or proof that plaintext never reaches disk.

A valid encrypted snapshot is still a point in time. It cannot prove freshness,
include later reservations, cancel an earlier model call or safely run beside
another active copy as one balance. Actual cutover requires writer fencing,
independent recovery-point evidence and reconciliation of later/uncertain work.

The coordinated account/CSV work in PR #102 is unchanged. A common encrypted
account/workspace/source/budget snapshot, a current external recovery witness,
offsite retention and host restore/drain verification remain open. Default/public
startup does not enable this tool and no real backup or live secret is read by
the tests.

## Offline evidence

`tests/test_budget_backup.py` uses actual AES-GCM and SQLite with synthetic data.
It checks the persisted ciphertext, original-database loss, fresh nonces, wrong
keys/policy/pins, tampering even with a recomputed digest, invalid encrypted
database payloads, size bounds, changed reads, existing destinations/sidecars,
write failure, POSIX directory durability and unavailable optional encryption.
A fresh process verifies that web imports do not activate the optional
module/library. Unit network calls are forbidden.

`tests/test_budget_backup_http.py` restores an uncertain encrypted ledger and
starts fresh authenticated loopback HTTP apps: new owner jobs remain blocked
without an SDK send. Other snapshot, budget, pricing and SDK regressions remain
required; these tests prove neither model quality nor deployed durability.

### Erfolgsgrenze nach SQLite-Close

Der Restore-Erfolg wird erst nach dem Schließen der neu geöffneten SQLite-Verbindung
bestätigt. Die ausschließlich erzeugte Zieldatei wird vor dem Schließen an Gerät,
Inode, Linkzahl und die entschlüsselten Snapshot-Bytes gebunden und anschließend
noch einmal gegen dieselbe Identität und dieselben Bytes geprüft. Ein Austausch des
Pfadnamens während des Close-Fensters – auch durch bytegleichen Inhalt – führt damit
zu einem fehlgeschlagenen Restore statt zu einem Erfolgs-Receipt.

Dieser Nachweis ist ein synthetischer Dateisystemtest. Er ersetzt weder
Produktions-Writer-Fencing noch unabhängige Freshness-/Anti-Rollback-Nachweise.

