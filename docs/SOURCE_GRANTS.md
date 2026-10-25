# Bounded operator-reviewed text source grants

Trusted startup code can pass validated `WorkspaceSourceManifest` values as an
immutable `source_manifests` tuple to `OwnerPilot`. Authenticated owner HTTP
admission now selects the exact current workspace grant and passes its bundle to
`TankAI.run(..., supplied_sources=bundle)`. The default/public application does
not enable this path. This implementation and its synthetic offline tests do not
configure real providers or approve inference/search or a production pilot.

## Immutable snapshots

`SuppliedSource` pins title, canonical HTTPS URL metadata, exact UTF-8 text and its
SHA-256 digest. Text must be nonempty, valid UTF-8, at most 8,000 bytes and contain
no ASCII controls apart from line breaks/tabs. Titles and URLs are bounded and
exclude Markdown boundary syntax, credentials, query strings and fragments.
URL checks are lexical: no hostname is resolved and no URL is fetched. They do
not prove that a hostname resolves to a public IP or authentic publisher.

`ApprovedSourceBundle` holds a stable operator approval identifier and one to five
immutable snapshots, at most 24,000 text bytes in total. Duplicate/colliding short
source IDs and ambiguous repeated URL metadata are rejected. IDs derive from the
title, URL and content digest, so changed text gets a different ID. Document text
is omitted from ordinary representations. Fresh prompt evidence records carry
the exact text, content digest, approval ID and `fetched=False`. Text boundaries
are escaped and embedded canonical source markers are neutralized; a document
cannot create a real source record or modify the immutable bundle. These prompt
rules reduce instruction confusion; they are not a proof against prompt injection.

Checksums prove byte identity, **not truth, factual accuracy, freshness, authorship,
copyright permission or cryptographic signature**. An approval ID is an operator
label, not authentication or evidence that a human actually reviewed a document.

## Scope and file pin

`WorkspaceSourceManifest` pins canonical owner, tenant and workspace UUIDs and a
nonempty validity window of at most seven days. `select(..., now=...)` returns the
bundle only for the exact identity tuple and within the half-open time interval.
It deliberately does not resolve sessions or roles. `OwnerPilot` freshly checks
session/membership and requires each grant's owner, tenant, workspace and entire
window to fit its approved pilot scope. Duplicate workspace grants are rejected.
No matching grant or an inactive grant blocks before runtime/job/model creation.
The check repeats at every model attempt, after counting/pricing and after the
committed money reservation. Expiry blocks later sends, including critic calls;
it cannot cancel a request already sent. An uncertain clock blocks admission.

`load_source_manifest(path, expected_sha256=...)` accepts a digest chosen by
trusted startup code and reads one regular JSON file of at most 128,000 bytes.
The digest pins all metadata and source text. Missing/invalid pins fail before
opening any file. Missing, empty, oversized, symlinked, changing or nonregular
files fail closed. No directory discovery, refresh, download or file write occurs.

The version-1 top-level fields are exactly `schema_version`, `owner_id`,
`tenant_id`, `workspace_id`, `valid_from`, `valid_until`, `approval_id` and `sources`.
Each source has exactly `title`, `url`, `text`, `sha256`. UTF-8 decoding, duplicate
fields at any depth, unknown/missing fields, unsupported schema versions,
nonfinite JSON values, noninteger timestamps and mismatched text/file digests
are rejected. Errors do not echo source contents or private filesystem paths.
File handles are bounded and checked before/after reading; on supported systems
no-follow and nonblocking flags also prevent symlink/FIFO reads.

This is neither an upload API nor a source approval endpoint. A browser-supplied
path, digest, document or matching metadata must not become trusted configuration.
There is no automatic provider, budget, secret, permission or production change.

## Source-only Core execution

Every specialist role receives the approved text, including analysis/code/writing
roles; changing the planner's role cannot bypass evidence checks. This path does
not describe or invoke tools, fetch URLs, call a search backend, or retrieve old
short-term/long-term/procedural memory as if it were currently approved evidence.
A Core instance rejects overlapping runs before resetting counters or replacing
its current bundle. Cleanup removes the transient grant even after an error.

The independent critic receives the same approved text for both step and final
checks; source IDs alone cannot override its content veto. Deterministic gates
require at least one current source citation and reject unknown or malformed
source markers, even when the critic approves or the ordinary evidence flag is
disabled. They **do not prove that every claim is supported or factually true**.
Citation checking, a model critic and operator review are distinct checks.

Receipts retain source IDs, URLs, content digests, approval ID and supplied mode.
The final catalog labels the data as supplied text without a current web fetch.
The full result is saved normally, while extra model consolidation/procedure
promotion is explicitly deferred with no retry. Source-bearing specialist and
critic prompts pass through the same complete-request size/token/pricing guard;
large content is never an exception to monetary or request limits.

## Offline verification

Run `python -m pytest tests/test_source_manifest.py tests/test_supplied_sources.py tests/test_owner_source_evidence.py tests/test_web_owner_pilot.py -q`.
The manifest suite blocks DNS and socket connections and checks exact identity/time selection, immutable content,
changed file/text/owner metadata, duplicate/ambiguous JSON, byte/count ceilings,
source-ID collisions, malformed input, missing pins, changing file metadata and
real symlink rejection where symlink creation is available. Core tests use
artificial documents and in-process models, with network connections forbidden.
HTTP tests allow only the loopback test server, use local SDK doubles and
synthetic tariffs, and cover pinned-file consumption, current sources for both
critic checks, cross-workspace isolation, expired/missing grants, expiry during
counting/after commit/after a response, independent critic rejection and citation
bypass attempts. Existing research, SDK and budget tests remain regression checks.

Before an actual pilot, separately review and pin real source contents and their
rights/freshness, verify current tariffs/counters and durable host storage/restore,
review upstream recovery/drain behavior, and obtain concrete pilot approval.
There is no upload/approval UI, grant renewal, automatic file reload, live fact
verification, encryption or cryptographic publisher signature in this increment.
