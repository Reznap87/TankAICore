# Owner-bound web admission and conservative restart blocking

Trusted startup code can supply an `OwnerPilot` to
`AppContext.from_env(server_host, owner_pilot=configuration)`. This is an explicit
programmatic path, not an environment flag, HTTP setting or live-pilot approval.
The default application startup does not enable it. The supplied existing ledger
is caller-owned and must outlive all active application/runtime leases.

## Identity and send authority

Configuration pins one canonical owner UUID, tenant UUID, at most 32 workspace
UUIDs, a shared initialized ledger and reviewed tariffs/endpoints/offline token
counter. It never creates or renews a pilot policy. Initialization rejects
disabled session authentication and the development queue, preventing an
alternate unbudgeted queue path in this mode.

The existing HTTP session and CSRF checks still run first. Before acquiring a
runtime for `/api/run`, the pilot verifies the exact configured owner, tenant,
approved workspace and owner role. It reloads the session and workspace access
from `AuthStore`: a cached/forged context, expired/revoked session, demotion or
workspace switch cannot retain spend authority. Checks repeat on each model
attempt, after potentially slow offline counting/pricing, and after reservation
commit immediately before dispatch. This does not cancel a request already
admitted before a subsequent revocation.

The browser supplies only goal/definition of done and a false parallel flag.
Identity, job IDs, tariffs, token/output limits, source grants and budget values
are rejected as client overrides. Each job ID is generated on the server and
includes the authenticated tenant/workspace IDs plus a fresh UUID. Main and
independent critic targets are checked before a job is created or money spent.

Pilot runtimes require the reviewed guarded adapters, a distinct critic, local
hashing embeddings, one verified plan step, no execution retries and five model
attempts. Provider adapters require monetary admission even when referenced
outside the scope. Raw references and an in-memory call-counter reset cannot
authorize a send. Runtime-cache eviction/recreation does not replace the ledger.
External research is disabled; the configured evidence requirement is preserved,
not silently relaxed. Approved source content is a separate unfinished gate;
this increment does not provide a live evidence mode.

## Persistent uncertainty

`begin_job()` commits the existing SQLite active-job slot before execution. After
a normal, fully returned run, scope cleanup closes that slot but keeps every
unreconciled monetary hold. A transport/parsing/settlement failure leaves the
active job intact, even when the Core catches the provider exception. Immediate
post-commit authorization failure conservatively retains the hold and slot too.

If the process exits abruptly, cleanup does not run: both the committed job and
any held attempt survive. Every later HTTP request gets a new server-generated
ID; the shared single-job limit blocks it across approved workspaces, fresh
sessions, new application objects and reopened ledger connections. No elapsed
deadline, timeout, cache eviction or browser retry automatically frees the slot.

There is intentionally no HTTP resume, unlock, renewal, settlement or reset
endpoint. Existing low-level ledger primitives are for separately reviewed
operator reconciliation, not proof that upstream work stopped. An operator or
administrator can still replace/roll back files or misuse those primitives;
distributed storage, anti-rollback, real-host restore and recovery approval are
outside this change. Do not close an uncertain slot merely to get requests running.

## Result preservation without a sixth model call

The Core records the full episode in long-term storage. In a guarded run it
defers model-backed semantic consolidation/procedure promotion, records an
explicit `memory_consolidation_deferred` receipt, and schedules no retry. It does
not silently substitute heuristic knowledge or start an additional provider call
after the five-attempt answer/verification path. Later authorized memory work
requires its own admission and is not implemented here.

## Offline verification and remaining gates

`tests/test_web_owner_pilot.py` exercises real local HTTP login, cookies, CSRF,
AuthStore, runtime creation and the complete Core with in-process SDK doubles
and artificial tariffs. It covers cross-user/workspace rejection, demotion,
stale/expired/revoked sessions, revocation during counting and after commit,
global active-job limits, unknown prices, fresh application/ledger instances,
preserved memory/results and abrupt subprocess exit inside an admitted attempt.
No real credentials, provider inference or external search is required.

Default/public startup remains unchanged. Before any real pilot, verify current
all-in tariffs and complete-request token ceilings, approved owner/workspace and
source configuration, actual host persistence/restore, lifecycle/drain handling,
upstream routing/cancellation behavior, and obtain concrete pilot approval.
This is single-host coordination and is not readiness evidence for production.
