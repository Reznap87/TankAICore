# Public registration

The release configuration in `wrangler.jsonc` enables
`TANKAI_ALLOW_REGISTRATION`. This is a source configuration, not evidence that
the release has been deployed. The Worker passes the flag to the container, and
the chat page offers registration only when the health endpoint reports it
enabled. Existing users can still sign in.

Registration is limited through the existing auth-state Durable Object: three
attempts per client per UTC hour, and fifty successful or unresolved registrations
per UTC day. Client identifiers are truncated 128-bit HMACs; raw client IPs are
not stored by this limiter. Hourly shards have bounded capacity and may reject new
clients when full. Explicit application rejections release their daily reservation;
successful responses and uncertain failures retain it. Limits survive restarts.

Login and registration accept `application/json` (including charset parameters)
and reject browser requests marked `Sec-Fetch-Site: cross-site`. Non-browser
clients may omit Fetch Metadata; they still need the JSON content type. Other
application endpoints retain their existing request handling.

The existing auth snapshot transport and its 8 MiB cap remain in use. This is a
bounded initial release, not an unlimited account service. There is currently no
email ownership verification. Keep these constraints visible in release review.

Validation includes Python HTTP account creation and subsequent login, actual
Worker forwarding with a controlled backend, persistent limiter tests, and UI
handler tests with a synthetic DOM. The local Edge test covers native form
validation, registration, visible account/workspace, logout and login. The public
deployment still needs its own verification.

The optional local container restart smoke check uses only synthetic accounts and
an in-memory HTTPS test adapter. With Docker, OpenSSL, and the baseline commit
available, run:

```sh
python .github/scripts/registration_auth_restart_smoke.py --baseline-commit b9eb349363889efe90e84460258ee19f0892b003
```

`--expected-head <full-sha>` optionally checks the current checkout. The check
builds from the working tree, which should be clean when attributing a result to
that commit. CI must fetch the baseline commit and make OpenSSL available. This
test does not exercise the production Cloudflare Durable Object or real accounts.

The browser check uses an existing Edge executable and Node's built-in WebSocket
client (Node 22.4 or newer; tested locally with Node 26.5.1). It needs no separate
automation package or browser download. Set `TANKAI_TEST_PYTHON` to the project
Python executable and `TANKAI_TEST_EDGE` to the Edge executable, then run
`node scripts/test-browser-auth.mjs`. It creates a temporary local database and
isolated Edge profile, then stops the test processes and removes those files.
After registration and login it also compares authenticated health/account
responses and checks the visible simulation mode for the active account/workspace.

The browser also sends a direct chat request while the test server explicitly
uses the mock provider. It checks the visible HTTP 503 rejection, absence of an
answer card or successful history entry, and recovery of the send button. This
validates the missing-model error path; it does not perform a real model call or
prove public registration or live chat in production.
