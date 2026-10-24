# Shared monetary reservation foundation

This change implements the local ledger required by Masterplan 0.11.7, under
Issue #25. It is a standalone foundation, not live-provider activation. Existing
provider adapters, web admission and production configuration do not use it yet.

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
creation; it never resets an existing ledger. Every transaction revalidates the
schema, immutable policy and bounded state. Unapproved tables, views and
triggers are rejected, including ordinary `sqliteX...` names that a SQL `LIKE`
underscore wildcard would incorrectly hide. The private prototype's wildcard
check is corrected in this change; that separate branch is not overwritten.

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

## Verification

Run locally without provider credentials or network inference:

```sh
python -m pytest tests/test_monetary_budget.py -q
```

The suite covers independently visible commits, shared main/critic/fallback
balances, concurrent connections and independent Python processes, abrupt
process exit, reopen/replay, job/day/total limits, unknown outcomes, settlement
overruns, expiry/clock rollback, invalid input and manipulated SQLite schemas.
The five regression cases for SQLite-like object names fail against the private
prototype's old filter and pass with the literal-prefix check.

## Remaining gates

- Bind the ledger to **every actual** SDK send and disable or individually admit
  hidden retries; the older in-memory call counter is not a money guard.
- Verify full input/output token bounds and current all-in tariffs (tax, currency
  conversion and every other billable item included). Unknown costs block sends.
- Bind real owner/tenant/workspace/job identity at admission and on each attempt.
- Configure trusted storage and prove restart and application restore in the
  actual deployment. A passing local reopen test is not production durability.
- Audit upstream routing/retries and explicitly approve the bounded pilot before
  any provider activation or real request.

This is a **single-host** ledger, not distributed coordination across Cloudflare
containers or hosts. Do not use independent per-container copies or network
filesystem replicas as one shared balance. An administrator can roll back or
replace files; this module does not provide an external anti-rollback anchor.
No deployment, provider setting, secret, dependency or scheduled task is changed.
