# Local monetary-ledger snapshots and isolated recovery

`MonetaryBudget.export_snapshot()` and `MonetaryBudget.restore_new(...)` are
explicit operator primitives for the single-host ledger. They have no HTTP
endpoint, environment activation, scheduler or production cutover. The default
application does not call them. They do not initialize or renew a pilot, settle
charges, close active jobs or authorize any provider request.

## Consistent bounded image

`export_snapshot()` returns a complete SQLite image as bytes, at most 8 MiB. It
holds the existing validated reservation transaction while a separate read-only
connection uses SQLite's backup API. This excludes other ledger writers during
the copy and includes committed WAL pages; copying the main database file alone
is insufficient. It does not back up the connection holding the write lock,
which would wait on its own transaction.

Logical database size is checked before backup allocation; backup progress also
has size and time limits. Source file identity is rechecked after the copy, and
the image must pass the full ledger/schema/relationship checks with the original
policy. The snapshot preserves exact jobs, attempts, reservations, settlements,
last-observed clock and sticky halt. Export does not release any money or job.

The transaction coordinates ledger writers, not arbitrary administrators who can
replace files or bypass this module. The reservation transaction's existing
SQLite lock timeout still applies before the bounded copy starts.

## New isolated destination only

`restore_new(path, data=..., expected_sha256=..., expected_policy=..., clock=...)`
requires exact immutable bytes, a separately trusted lowercase SHA-256 pin and
the complete independently approved `PilotPolicy`. Size, header, digest and
policy types are checked before restoration. A scratch copy passes the existing
strict ledger validation, rather than just SQLite's integrity check. A valid
digest does not allow a foreign schema, altered policy or inconsistent state.

The destination must be new, with an existing trusted parent directory and no
preexisting `-wal`, `-shm` or `-journal` companion. Existing files, symlinks,
directories and sidecars are never deleted or replaced. Exclusive creation
also rejects a concurrently created destination. The new image is flushed and
synced before it is opened and revalidated as a ledger. On POSIX, the parent
directory is synced as a separate durability boundary before success. It must
still have
exactly one filesystem link; a concurrently created hard-link alias aborts the
restore before the ledger is returned. A failed write can leave
a diagnostic file; a retry must fail rather than replace it. Windows directory
metadata durability and actual-host power-loss behavior remain operator/filesystem
responsibilities.

Held/unknown attempts keep their full exposure and replay denial. Active jobs
remain active and block another owner HTTP job after fresh application, database
and process starts. Confirmed charges, spent attempts/job quotas, daily/total
limits, observed expiry/clock regression and pricing-overrun halts are retained.
Recovery is not evidence that an earlier upstream model request has stopped.

## Trust and remaining deployment work

The returned image is **not encrypted** and contains owner IDs, billing metadata
and amounts. This step uses only synthetic data in private temporary test paths.
An optional operator wrapper now encrypts and validates the persisted ledger
backup: [ENCRYPTED_BUDGET_BACKUP.md](ENCRYPTED_BUDGET_BACKUP.md). The raw snapshot
primitive remains plaintext; coordinated workspace/account recovery is separate.
Real backup storage still needs protected keys, access controls, retention and
an independently stored recovery pin. A checksum computed
from an untrusted image is not authentication or evidence that the image is the
latest one.

A consistent snapshot is a point in time. Reservations committed after it are
not in it. Never replace a live ledger with a snapshot or run both copies as one
balance. Before an actual recovery, stop/fence all old writers, establish an
independent current high-water mark and reconcile later/uncertain attempts. This
module does not implement that cutover or an external anti-rollback anchor.

Coordinated account, workspace memory/history/vector, source grant and budget
backup remains separate work. In particular, this does not modify the account/
CSV recovery work in PR #102 or claim that the current Cloudflare deployment has
durable shared storage. Real-host restore/drain verification and concrete pilot
approval remain required.

## Offline evidence

`tests/test_budget_snapshots.py` exercises real SQLite WAL backup, writer exclusion,
round-trip identity, pending/unknown/settled outcomes, replay protection, halt and
clock/job/budget limits, invalid pins/policy/schema, size bounds, write failure,
destination races and preservation of existing files/sidecars. A fresh Python
process verifies that restored uncertainty still blocks a new job.
POSIX regressions additionally require directory metadata to be synced before
backup or restore success can be acknowledged.

`tests/test_budget_recovery_http.py` uses actual session/CSRF/HTTP owner admission
with a restored unknown hold and fresh app/ledger instances: no SDK send occurs.
Documents, prices and credentials are artificial; only the loopback test server
is reachable. The unit suite blocks DNS and network connections entirely.
