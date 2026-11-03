# Public Cloudflare recovery: verified state and remaining gate

Verified 2026-10-28 through the newly connected target account. This is a read-only
inventory and a correction to the source readiness check, not a production restore.

## Actual deployed resources

- Worker `tankai-core` owns both `tankaicore.com` and `www.tankaicore.com`.
- Current Worker version: `c89bd12a-2b01-4a81-af96-108ca7487df2`, at 100%.
- Current container image digest:
  `6207b203123c8124ba7e00697ba5047e08bea4c4f50a4331ca2423eb04901235`.
- Successful deployment run `40092122086` records both this Worker version and
  image digest, from main `b9eb349363889efe90e84460258ee19f0892b003`.
- Container application `tankai-core-tankaicontainer` uses scheduling policy
  `default` and maximum one instance.
- `TANKAI_AUTH_STATE` binds `TankAIAuthState`; its SQLite-backed namespace has an
  object reporting stored data. `TANKAI_CONTAINER` binds `TankAIContainer` and has
  its own SQLite-backed namespace. Namespace existence/storage metadata does not
  establish which application records were restored.
- Only the two DO bindings and the existing auth-state/bootstrap secret names
  were returned by Worker settings. No secret values were requested or retained.

HTTPS checks returned 200 for `/api/health`, 401 for unauthenticated
`/api/auth/me`, and 403 for unauthenticated `/_tankai/auth-state`. The app reports
session authentication, registration disabled and `production_ready=false`.
No provider-enabled binding was present; the inspected deployed Worker contract
defaults to the mock adapter. This check requested no model response.

## Why a complete public recovery is still unproven

The current Worker persists an auth SQLite snapshot in `TankAIAuthState`. Its
container filesystem, workspace memory/history and any future monetary ledger
are separate. `TANKAI_RUN_STORE` points into `/tmp`; the auth-state module itself
explicitly excludes workspace/run-history persistence. The container DO is a
lifecycle controller; stored controller metadata is not a backup of its disk.

Cloudflare documents that container disk is ephemeral by default. Its current
filesystem snapshots require the `durable_object` scheduling policy, which this
application does not use. A snapshot is also tied to the exact image version and
does not capture running processes. Therefore neither the auth DO nor a nominal
snapshot feature may be treated as current whole-system restore evidence.

Sources checked 2026-10-28:

- https://developers.cloudflare.com/containers/concepts/architecture/
- https://developers.cloudflare.com/containers/guides/snapshots/
- `src/cloudflare.ts`, `wrangler.jsonc`, `tankai/web/auth_state.py`.

## Readiness check correction

`production_live_provider_readiness.py` previously reported READY for a fully
wired independent provider target plus matching secret names. That is insufficient.
The regression reproduces that false positive. The evaluator now identifies its
scope as source/presence metadata only and always leaves these unsupported facts
UNKNOWN:

- deployed shared monetary reservation before every provider send, valid complete
  prices/token bounds and durable uncertain holds;
- coordinated account/workspace/budget recovery, freshness and old-writer fencing.

The CLI exits 2 for this UNKNOWN state. No source marker, environment flag or
secret-name list can turn it into production authorization. A future operational
verifier is required before either fact can become PASS. This change does not
implement persistence or authorize deployments, provider calls or search.

## Next bounded implementation

First implement and test a coordinated application-data checkpoint/export contract
covering auth, workspace files/history, source grants and the monetary ledger. Use
the existing recovery and ledger snapshot contracts, drain all writers, preserve
uncertain holds and bind every component to one pinned generation. Inspect PR #102
before changing account/CSV recovery to preserve its independent work.

Then choose and review the actual durable storage/restore route. Relying on native
container snapshots requires a separately prepared scheduling/API migration and
an exact-image, quiescent restore test; adding R2/FUSE alone would not prove SQLite
transaction or cross-component consistency. Do not make either production change
as an inventory side effect.

Before the public rehearsal, establish an export of the currently authoritative
data and rollback point. Restore into an isolated destination and verify through
the application without a model call. Only a separately approved public cutover
may stop/restart the public container or write restored data. Account/session
state, model quality, private Hetzner recovery and public whole-data recovery
remain distinct claims.
