# Shared monetary reservations and opt-in SDK admission

This change implements the local ledger required by Masterplan 0.11.7, under
Issue #25. Trusted application code can now supply a `MonetaryDispatch` to
`TankAI(..., monetary_dispatch=...)` (or `LLMCallBudget`) to protect the existing
OpenAI and Anthropic text adapters. A programmatically supplied `OwnerPilot` now
binds the same guard to session-authenticated `/api/run` admission; see
[the owner admission contract](OWNER_PILOT_ADMISSION.md). Default startup and
production configuration do not activate it. This code is not live-provider approval.

## Contract

`tankai/core/monetary_budget.py` stores one immutable owner-pilot policy, jobs and
request attempts in a single local SQLite database. Amounts are integer
micro-euros. The largest allowed policy is EUR 0.20 per job, EUR 2 per pilot day
and EUR 10 in total over at most seven 24-hour periods from the declared start.
Only one job can be active; daily/total job limits, a five-attempt ceiling and a
120-second job admission deadline also apply. These are configuration ceilings,
not permission to spend.

All main, specialist, critic, retry and fallback paths must share the **same
database and job ID**. Separate connections and local processes serialize via
`BEGIN IMMEDIATE`. A successful new reservation is committed with
`synchronous=FULL` before `dispatch_allowed=True` returns. A repeated attempt ID
is only a lookup and never grants another send. Reuse for a different job or
quote is rejected. Closing a job does not refund unresolved attempts or cancel
upstream work. A clock rollback cannot restart an observed expired pilot.

Missing, empty, corrupt, incompatible or unexpectedly changed state blocks new
reservations. Only `create()` explicitly initializes a new file, with exclusive
creation; it never resets an existing ledger. On POSIX, creation also syncs the
parent directory after the SQLite commit and before returning the initialized
ledger. A file-content commit without a durable directory entry is not reported
as successful creation. Windows directory-metadata durability remains an
operator/filesystem responsibility. Every transaction revalidates the
schema, immutable policy and bounded state. Unapproved tables, views and
triggers are rejected, including ordinary `sqliteX...` names that a SQL `LIKE`
underscore wildcard would incorrectly hide. The private prototype's wildcard
check is corrected in this change; that separate branch is not overwritten.
The database must also have exactly one filesystem link before opening and
before every transaction. A hard-linked alias could otherwise address the same
SQLite inode through different WAL/journal sidecar names and evade path-bound
writer coordination; creating such an alias on an open ledger fails closed.

An explicit, currently valid `PriceQuote` must describe the conservative all-in
ceiling of one bounded request. Missing/expired/future quotes and zero-price
claims are rejected. A source URL is metadata, **not verification of its price**.
The ledger makes no network requests and cannot authenticate a tariff, count
tokens or compare an invoice to the request. Synthetic test quotes authorize
no real calls.

Unknown outcomes keep the full reservation, including after restart or job
closure. Only trusted separate reconciliation may call `settle()` with a
confirmed charge no larger than the reservation. A reported higher charge
retains the hold, raises an error and permanently halts new admissions; there
is no automatic resume, credit top-up or quota reset. Such a halt cannot undo
money already billed upstream.

## Bounded text dispatch

`request_dispatch.py` freezes the final SDK parameters and sends a fresh copy
only after an independently visible committed reservation. The trusted offline
input counter receives the entire request, including system text and framing.
It must prove a conservative bound for the exact model; there is deliberately
no default tokenizer or guessed character-to-token ratio. Input ceilings above
12,000 tokens, output ceilings above 2,048 tokens, non-text messages, additional
request fields, and serialized requests above 128,000 UTF-8 bytes are blocked.

After the final session/source authorization, the dispatcher rechecks its target
tariff and calls `MonetaryBudget.check_dispatch()` before invoking the SDK. The
ledger revalidates the durable hold, owner/job binding, active job, pilot halt,
monotonic observed time, pilot expiry, job admission deadline and stored quote
validity. Time spent committing the reservation or authorizing the caller cannot
silently retain the earlier approval. A job closure or pricing halt committed by
another connection before this recheck also blocks the send. A late rejection
keeps the full hold and stops the dispatcher; it grants no retry or refund.
`check_dispatch()` creates no attempt and grants no replay permission: only the
caller of a fresh `reserve()` with `dispatch_allowed=True` may send once. This
is an admission check before SDK invocation, not a guarantee of completion or
upstream cancellation at the deadline.

`request_pricing.py` requires explicit reviewed EUR/USD tariff components,
validity, sources, any FX ceiling, taxes, request fees and payment surcharges.
It computes the all-in ceiling with exact rational arithmetic and rounds upward
to integer micro-euros. Missing coverage, stale prices, aliases, ambiguous
numbers and zero-price/free-quota assumptions cannot authorize dispatch.

Main, critic, specialist and retry/fallback calls made through the same protected
Core share the ledger/job. Unreviewed delegates and callers replacing the
internal context are blocked; raw references to a protected adapter also require
admission. Resetting the legacy in-memory call counter cannot refund money or
reset the ledger's five-attempt ceiling. Unprotected legacy construction keeps
its previous behavior and is **not** a monetary-safe production configuration.

Each protected invocation uses a dedicated default HTTP transport with transport
and SDK retries disabled, redirects disabled, and environment proxies disabled.
SDK timeouts are capped at 30 seconds per I/O operation; this does not guarantee
a 30-second total request or cancellation of upstream computation. Tested SDKs
are OpenAI 3.37.0 and Anthropic 2.3.0 (development dependencies only). Anthropic
SDKs which removed temperature receive no such parameter; explicit unsupported
overrides are rejected rather than silently dropped. No automatic SDK fallback
is attempted. SDK imports, the transport implementation, trusted application
code, model token accounting, endpoint infrastructure and upstream billing
remain trust boundaries.

The protected transport caps each HTTP response body at **256,000 bytes** before
SDK JSON parsing, including error bodies and metadata. It counts streamed chunks
even without `Content-Length` or with a false small length. An oversized chunk
is rejected before entering the SDK's body buffer; the remaining stream is
closed. Invalid or oversized declared lengths are rejected without reading the
body. The transport requests `Accept-Encoding: identity` and rejects other
content encodings before reading. It also rejects compressing or combined HTTP
transfer codings; ordinary `chunked` framing remains allowed and its yielded
body bytes are counted. This prevents decompression after the byte cap.
Returned text has a separate **128,000 UTF-8 byte** ceiling; invalid Unicode and
oversized text fail before entering the Core. The requested 2,048-token ceiling
still applies independently. Byte limits do not prove actual token usage,
upstream charges, a total elapsed-time limit or upstream cancellation.
Limits apply only to protected dispatch; default web imports do not require
optional SDK/HTTP libraries. No compression fallback or extra send is attempted.

Normal responses, malformed responses, errors and cancellations retain the full
hold; token usage never refunds it automatically. A send/parsing/settlement
failure blocks subsequent sends through the same dispatcher, including queued
siblings and a critic fallback. This stop flag is process-local, while the
reservation/replay protection is durable. A new dispatcher must not be treated
as permission to resume an uncertain job. The owner web admission now preserves
the durable active-job slot on uncertainty and generates a fresh job ID for each
new request, so a restart cannot bypass that slot. Operator recovery and proven
upstream completion/cancellation remain separate gates. The dispatcher
serializes its own job's sends; independently constructed dispatchers do not
share that in-memory lock, but their reservations still share SQLite limits.

## Verification

Run locally without provider credentials or network inference:

```sh
python -m pip install -r requirements-dev.txt
python -m pytest tests/test_monetary_budget.py tests/test_request_pricing.py tests/test_request_dispatch.py tests/test_request_dispatch_sdks.py -q
```

The suite covers independently visible commits, final-authorization time expiry
for both adapters, stops committed after reservation, shared main/critic/fallback
balances, concurrent connections and independent Python processes, abrupt
process exit, reopen/replay, job/day/total limits, unknown outcomes, settlement
overruns, expiry/clock rollback, invalid input and manipulated SQLite schemas.
The five regression cases for SQLite-like object names fail against the private
prototype's old filter and pass with the literal-prefix check.

Adapter tests execute the real Core pipeline with local SDK doubles and run the
installed SDKs against intercepted HTTP responses. Both providers are checked
for success, server error, timeout, redirect and malformed response: exactly one
HTTP attempt sees a committed hold, no SDK retry or redirect occurs, and no real
provider request is made. Synthetic credentials, prices and reserved `.test`
endpoints are used throughout. These tests prove the tested send paths, not
current provider prices, model quality or deployed runtime behavior.

`tests/test_response_limits.py` runs both installed SDKs against counted local
streams. It checks early close, absent/false/invalid lengths, encoded bodies,
exact body/text bounds, multi-byte output and invalid Unicode, and requires one
held attempt without a retry. `tests/test_response_limits_http.py` checks that an
output-limit rejection returns a bounded service error and preserves the active
job and money hold through a fresh authenticated HTTP app and ledger reopen.

## Remaining gates

The ledger now has explicit, locally verified bounded SQLite snapshot/export and
new-destination recovery primitives; see [BUDGET_SNAPSHOTS.md](BUDGET_SNAPSHOTS.md).
They preserve held attempts/active-job stops and include WAL commits. They do not
perform a live cutover, encrypt/store backups, prove freshness against rollback
or coordinate workspace/account state.
The separate optional [encrypted wrapper](ENCRYPTED_BUDGET_BACKUP.md) now protects
a persisted ledger snapshot with authenticated encryption and read-back validation;
it does not activate a coordinated runtime recovery or deployment key.

- Make protected dispatch mandatory at authenticated production admission for
  **every actual** path; direct legacy construction is still not a money guard.
- Verify full input/output token bounds and current all-in tariffs (tax, currency
  conversion and every other billable item included). Unknown costs block sends.
- Approve the actual owner/tenant/workspace identities at rollout; the explicit
  owner web path now enforces fresh session/membership checks on each attempt.
- Review and pin real workspace source grants before the pilot. The explicit
  owner path now rejects missing/expired grants and sends approved text through
  the same request and money guard; see [source grants](SOURCE_GRANTS.md).
- Review upstream completion/cancellation before any recovery/unlock. The web
  pilot blocks unknown active work durably; it has no automatic resume or reset.
- Configure trusted storage and prove restart and application restore in the
  actual deployment. A passing local reopen test is not production durability.
- Audit upstream routing/retries and explicitly approve the bounded pilot before
  any provider activation or real request.

This is a **single-host** ledger, not distributed coordination across Cloudflare
containers or hosts. Do not use independent per-container copies or network
filesystem replicas as one shared balance. An administrator can roll back or
replace files; this module does not provide an external anti-rollback anchor.
No deployment, provider setting, secret, production dependency or scheduled task
is changed. Development-only SDK packages support offline regression checks.
