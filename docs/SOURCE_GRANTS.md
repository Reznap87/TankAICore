# Bounded operator-reviewed text source grants

This increment implements offline validation and selection of pinned source
configuration. **Core, HTTP admission and `OwnerPilot` do not consume these grants
yet.** It does not enable a source mode, read real documents, configure providers,
relax evidence requirements or authorize inference/search. The next separate gate
is wiring valid grants to the authenticated owner/Core citation path without any
external search or fallback.

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
is omitted from ordinary representations. No prompt renderer is implemented here;
the contents must still be treated as untrusted data at future consumption.

Checksums prove byte identity, **not truth, factual accuracy, freshness, authorship,
copyright permission or cryptographic signature**. An approval ID is an operator
label, not authentication or evidence that a human actually reviewed a document.

## Scope and file pin

`WorkspaceSourceManifest` pins canonical owner, tenant and workspace UUIDs and a
nonempty validity window of at most seven days. `select(..., now=...)` returns the
bundle only for the exact identity tuple and within the half-open time interval.
It deliberately does not resolve sessions or roles. A future owner integration
must still freshly authenticate session/membership and intersect the grant with
the approved pilot policy; matching IDs alone are not permission to spend.

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

## Offline validation

Run `python -m pytest tests/test_source_manifest.py -q`. The suite blocks DNS and
socket connections and checks exact identity/time selection, immutable content,
changed file/text/owner metadata, duplicate/ambiguous JSON, byte/count ceilings,
source-ID collisions, malformed input, missing pins, changing file metadata and
real symlink rejection where symlink creation is available. Existing research,
owner admission and budget tests remain separate regression checks.
