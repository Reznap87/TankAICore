# Private CSV file jobs

An authenticated user can save a UTF-8 CSV, choose individual cleanup operations,
review a server-computed preview, and explicitly create an immutable CSV/report
pair. A saved draft can be reopened after a browser or server restart. This is a
deterministic file tool, not an LLM response. The task note is retained for the
user; it is not interpreted as instructions or sent to a provider.

## Enable only for the local private pilot

Set `TANKAI_FILE_JOBS_ENABLED=1`, `TANKAI_HOST=127.0.0.1` and
`TANKAI_AUTH_MODE=session` for a development server with its own persistent
`TANKAI_DATA_ROOT` and authentication database. Use an existing local session
account. The feature is off by default. Startup rejects enabling it on a public
bind address or with authentication disabled. Authenticated chat users see
**Dateiauftrag · CSV prüfen** in the sidebar; `/files` opens the file workflow.

This change does not enable uploads on tankaicore.com, change a production
setting, add provider credentials, or deploy an application. The SQLite store
is a single-host development persistence mechanism. Container replacement,
Cloudflare persistence, disaster recovery of the complete workspace and
multi-host coordination remain separate production gates.

## Processing contract

- Input: UTF-8, optionally with BOM; at most 2,000,000 bytes, 5,000 data records,
  40 columns and 100,000 characters per field. CRLF, LF, comma, semicolon and
  tab delimiters, quoted separators/newlines and escaped double quotes work.
- An uploaded BOM is retained in the stored original. Strings, including
  leading zeroes, dates and decimal separators, are never inferred as numbers.
- Trimming, blank removal and exact duplicate removal start **off**. Formula
  protection starts **on** and is explicitly selectable before confirmation.
- A physical blank record can be removed when allowed. A delimiter-only record
  with the wrong number of fields is still rejected. Other uneven records and
  invalid headers block completion; no cell is guessed or silently repaired.
- Exact duplicate comparison occurs after trimming only when trimming is chosen.
  The first matching record is retained; comparisons are case sensitive.
- Formula protection prefixes `'` to risky cells, including headers, whitespace
  followed by `=`, `+`, `-` or `@`, and leading tab/CR/LF. Negative numbers are
  included. This changes exported cell values and is shown in the report. It is
  a defensive text-export measure, not a guarantee about every spreadsheet app.
- Preview: at most 20 records, eight columns and 200 characters per displayed
  field. Export: all valid retained records, UTF-8 BOM and CRLF; at most 4 MB.
- The final JSON report contains counts, selected options, duplicate policy,
  original/export SHA-256, exact export byte length and `externalModelCalls: 0`.
  It does not contain cell values or claim semantic data correctness.
- Existing CSV downloads and the report are immutable once confirmed. Retry of
  completion returns the same saved result. Downloads do not recompute exports.

## Storage and isolation

`TANKAI_DATA_ROOT/csv-jobs.db` retains the source, chosen options and completed
deliverables in SQLite transactions. Each operation requires the resolved
session's tenant, workspace and user. Another member of the same workspace does
not gain access to this user's file jobs. Client-provided identities and paths
are not accepted. A UUID transfer key makes upload retries idempotent; reuse with
different input is rejected. Preview versions prevent stale tabs from completing
a changed draft. Mutations require CSRF and `X-TankAI-Workspace` binding; the UI
also checks the current account/workspace before actions. Downloads can bind an
expected workspace with the `workspace` query parameter. A workspace mismatch
fails without applying a change.

Admission is atomic: at most ten jobs per user/workspace, 100 overall and 40 MB
of combined source/export payloads. SQLite file overhead and transaction journals
are additional. Users can delete their jobs to reclaim admission capacity.
SQLite deletion is not secure erasure. Retention scheduling is not implemented;
operator-triggered encrypted backups are described below. The database is created with mode 0600 on systems
that support POSIX modes. Windows deployment needs appropriate directory ACLs.

No source text enters the chat history, logs or an external model. The UI inserts
file content through `textContent`; nonce CSP, authenticated downloads, no-store
and attachment headers remain enforced. The original client-only Tabellenhilfe
demo has a different data boundary: this integrated workflow sends the CSV to the
private loopback Core so it can save and resume a job.

## Validation

`python -m pytest -q tests/test_file_jobs.py tests/test_file_jobs_http.py`

Ten independently specified synthetic CSV cases assert exact expected bytes:
leading zeroes, quoted separators, escaped quotes, embedded CRLF, BOM/Unicode,
blank records, deduplication after trimming, formula cells/headers, explicitly
disabled protection and unchanged whitespace/duplicates. Other tests cover
malformed/oversized inputs, tenant/workspace/user isolation, authentication,
CSRF, stale revisions, workspace binding, concurrent admission, idempotency and
global quotas. The restore test uses SQLite's snapshot API and verifies that a
separately reopened restored database yields the exact same report and CSV.
It is a local restore proof, not a production backup claim.

The browser proof uses an isolated headless browser and a fixture account, checks
real downloads against independently expected bytes and their report, reloads
a selected draft, checks the BOM of the original file and verifies mobile layout.
Only synthetic data is used. No paid inference, search, training or deployment is
part of validation. Full CI remains necessary in addition to local checks.

## Encrypted operator backups and fresh-directory restore

Run these commands from the Core checkout. Install the optional operator dependency with
`python -m pip install -r requirements-backup.txt`. The web server does not import
the encryption package or expose backup routes. A local server operator with
filesystem access can run:

```text
python -m tankai.web.file_backup backup --source /private/data/csv-jobs.db --destination /protected/backups/jobs.tankai-backup --key-file /protected/keys/csv-backup.key
python -m tankai.web.file_backup verify --source /protected/backups/jobs.tankai-backup --key-file /protected/keys/csv-backup.key
python -m tankai.web.file_backup restore --source /protected/backups/jobs.tankai-backup --destination /private/recovered-csv --key-file /protected/keys/csv-backup.key
```

Paths are examples. Both parent directories must already exist and be under the
operator's control. The key file must contain exactly 32 randomly generated
binary bytes, not a password, text encoding or server credential. Keep it in
separate protected key storage, outside source control and separate from backup
copies. POSIX key files must be inaccessible to other accounts; Windows requires
appropriate directory/file ACLs. The tool does not generate a deployment key,
rotate credentials or configure a scheduler. Losing the key prevents recovery.

The backup uses SQLite's online snapshot API, so committed WAL changes are
included and uncommitted writes are excluded without stopping the application.
Before encryption, the snapshot must have the exact supported v1 table/column/
index structure, pass the integrity and quota checks, and reproduce every
completed export and its report from the saved source/options. Unexpected tables,
views, triggers, invalid identifiers or inconsistent deliveries fail validation.
For a standalone snapshot, the serialized journal-mode header is set to rollback
mode as prescribed by SQLite; the source database's journal mode is not changed.

The envelope is `TANKAI-CSV-BACKUP\0\1`, a fresh 12-byte nonce and AES-256-GCM
ciphertext with its authentication tag. The fixed format/cipher/scope header is
authenticated as associated data. There is no compression or archive extraction.
Only encrypted bytes are written during backup creation; the snapshot stays in
process memory. The actual saved file is read back, authenticated and compared
with the snapshot before success is returned. Snapshot size is limited to 64 MiB
including SQLite overhead, with a 15-second snapshot deadline. Read size bounds
also apply to encrypted input and key files.

`verify` authenticates and validates the saved file without creating an output.
`restore` performs those checks **before** creating its destination. It requires
a new directory and writes only `csv-jobs.db` there. An existing directory, file
or journal sidecar is never replaced. The new file is flushed and reread; success
includes its verified snapshot hash. A failed write returns an error and may
leave an incomplete new restore directory; inspect it and choose a different new
destination for a retry. On supported POSIX systems, files start at mode 0600
and the new restore directory at mode 0700. Windows ACLs remain an operator duty.

JSON receipts contain operation, format, cipher, job counts, byte counts and
hashes. They contain no cell values, filenames, key material or passwords. The
backup covers **only the CSV job store**. Authentication, workspace history,
budgets and other state need their own coordinated recovery procedure. Restored
jobs retain the original tenant/workspace/user IDs and require those identities
to be available in the separately recovered authentication store. Switching a
running service to a recovered data root is a separate operator action.

This protects backup contents; the running SQLite database remains plaintext.
Automatic scheduled backups, offsite copies, deployment key storage and a full
production/Cloudflare recovery drill are separate work. Tests use ephemeral
synthetic keys and data. They include ten exact CSV deliveries, pending choices,
committed/uncommitted WAL writes, wrong keys, corrupted ciphertext, malformed
databases, output collisions and authenticated downloads from a fresh app context.
The fresh-context test retains the authentication database separately and proves
that another user still cannot access the restored file jobs.

```text
python -m pytest -q tests/test_file_backup.py tests/test_file_backup_http.py
```

References: [authenticated encryption in cryptography](https://cryptography.io/en/stable/hazmat/primitives/aead/),
[SQLite online backup](https://www.sqlite.org/backup.html), and
[standalone deserialization of WAL snapshots](https://www.sqlite.org/c3ref/deserialize.html).

Implementation references: [Python CSV documentation](https://docs.python.org/3/library/csv.html)
and [SQLite transaction handling in Python](https://docs.python.org/3/library/sqlite3.html).
