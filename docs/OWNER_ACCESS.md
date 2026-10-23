# Owner access on Cloudflare

The existing protected `Deploy to Cloudflare` workflow accepts two optional
inputs for first-time setup: `owner_email` and `owner_public_key` (base64 DER
RSA public key, RSA-OAEP SHA-256). Use both together. `confirm_production=DEPLOY`,
current `expected_sha`, protected main, successful CI, and the production
environment are still required. Ordinary deployments leave both inputs blank.

Provisioning generates a random password in the runner, installs only its salted
scrypt hash plus stable owner/tenant/workspace UUIDs in a Worker secret, and
creates a separate secret for the auth-state transport. It refuses to overwrite
either secret on a subsequent attempt. The initial email/password JSON is printed
only as RSA-OAEP SHA-256 ciphertext. Keep the matching private key outside the
repository and GitHub. There is no plaintext password artifact or log output.
If the subsequent deployment fails, retrieve/decrypt the ciphertext from the
original run and retry deployment with the two owner inputs blank.

On startup the container restores the SQLite auth database from a separate,
SQLite-backed Durable Object before opening it. When the database is empty it
creates the secret-configured owner. It never resets an existing account on
restart. After each POST, committed auth changes are backed up before any success
or session cookie is acknowledged. An unavailable backup returns 503. A revision
check prevents stale writes from overwriting newer snapshots.

The internal snapshot route requires the Worker secret as a bearer token, rejects
anonymous access, and never starts the Core container. Data is split into 64 KiB
chunks, atomically stored with its revision, and capped at 8 MiB. This contract
supports the configured single Core container; multiple independent writers
require a different storage adapter. Snapshot or restoration errors fail closed.
Before replacement, a restored file must also pass SQLite integrity checking,
contain every required TankAI authentication table and column, declare the current
authentication schema version, and have no broken foreign-key relationships. Required
authentication objects must be ordinary tables rather than views, and snapshots may
not attach triggers to them. A
healthy but foreign or incompatible SQLite file is rejected without replacing the
local account database.

This persists accounts, memberships, sessions, audit events and auth rate-limit
records. It does **not** persist workspace files or run history, and does not
enable a live LLM provider or public registration. Sessions created only by a
standalone administrative CLI must be explicitly saved when using remote state;
the deployed bootstrap and web server handle that automatically.
