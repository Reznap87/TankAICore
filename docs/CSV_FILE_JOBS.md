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
SQLite deletion is not secure erasure; retention and encrypted backups are not
implemented by this feature. The database is created with mode 0600 on systems
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

Implementation references: [Python CSV documentation](https://docs.python.org/3/library/csv.html)
and [SQLite transaction handling in Python](https://docs.python.org/3/library/sqlite3.html).
