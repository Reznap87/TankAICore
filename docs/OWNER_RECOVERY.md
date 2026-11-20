# One-shot Owner Recovery (inactive until operator authorization)

Recovery is deliberately disabled unless the protected Worker secret `TANKAI_OWNER_RECOVERY` is supplied. It is not an HTTP endpoint and does not enable public registration, paid calls, or OpenRouter-Free.

## Secret policy

The secret must contain a JSON object with exactly four fields: `recovery_id` (canonical UUID), `email` (the existing bootstrapped owner email), `expires_at` (Unix epoch seconds), `password_hash` (scrypt hash created locally with `tankai.web.auth.hash_password`). Never submit a plaintext password to GitHub or Worker secrets.

`TANKAI_BOOTSTRAP_OWNER` supplies the authoritative existing user, tenant and workspace IDs. The current DB must contain this active user with the specified email and owner membership in that workspace. A malformed, expired, or mismatched policy fails closed.

The password update, existing-session revocation, audit row and one-shot nonce marker in `auth_meta` are committed in one SQLite transaction. A replay of that nonce is a no-op, including after expiration or container restart. `AppContext.from_env()` must persist the entire updated auth DB through `AuthState.save()` before serving requests.

## Production rollout gate

1. Verify a restorable checkpoint for the current auth database **and** separate workspace files/history. The AuthState snapshot alone is not a whole-application backup; Cloudflare container disk may be ephemeral.
2. Generate an independent strong new password locally and create its scrypt hash locally. Generate a new UUID nonce and expiration no more than 15 minutes ahead. Keep the resulting secret policy outside Git and logs.
3. Install this JSON only into the Cloudflare secret `TANKAI_OWNER_RECOVERY`. Perform a controlled container deployment/restart only after backup, exact-SHA CI and the protected production workflow. Changing a secret does not itself prove the recovery has run.
4. Verify login, owner role and unchanged user/tenant/workspace IDs, and confirm old sessions are revoked. Remove the recovery secret immediately afterward.
5. Leave `TANKAI_ALLOW_REGISTRATION=0`. Do not enable paid providers or the monetary pilot.

## Test

`pytest -q tests/test_owner_recovery.py`

**This document is not production authorization.**