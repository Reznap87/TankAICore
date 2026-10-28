# Mandatory monetary admission for real web requests

The ordinary `/api/run` path may no longer dispatch a real adapter without an
explicit shared `OwnerPilot`. Previously the protected path existed, but default
startup left it optional: an authenticated user could reach the legacy route
and obtain a normal HTTP result without a monetary reservation.

After session and CSRF checks, body validation and workspace admission, the
server inspects both actual runtime adapters. If either main or critic is not
simulated and no owner pilot is attached, it returns HTTP 503 with
`error_code=monetary_admission_required` before running the Core or either model.
Mixed real/simulated pairs are blocked too. No browser payload, retry, parallel
flag or fresh session supplies a pilot. Denied requests are audited and do not
create a run-history entry. Account access and existing history remain readable.

Fully simulated requests retain their existing explicit simulation behavior.
The configured owner path continues through current membership, approved
sources, complete-request pricing, durable common reservations and the guarded
SDK dispatch. Its unknown outcomes and restored holds remain conservative.
See [OWNER_PILOT_ADMISSION.md](OWNER_PILOT_ADMISSION.md).

Public health metadata reports `live_model_requests_require_budget=true` and
whether `monetary_admission_configured` is true. A real adapter alone no longer
qualifies for `verification_ready`: a configured pilot is necessary too.
Configuration is not proof that current tariffs, sources or ledger admission
allow any particular request. `production_ready` remains false.

This is the web admission boundary. Direct trusted CLI/Core construction and an
independently exposed model-router API are separate interfaces; this HTTP change
does not impose a shared ledger on those independent services. No new provider,
price, token counter, source approval, ledger policy or permission to spend is
created by installing this code. Missing reviewed configuration therefore blocks
real web requests rather than using a synthetic or guessed tariff.

`tests/test_required_web_budget.py` covers the real and both mixed adapter pairs,
repeated denied requests, zero SDK calls, accessible login/history, session/CSRF
checks and truthful health metadata. Four assertions failed on the previous
implementation (HTTP 200 instead of 503 and missing policy metadata). Existing
owner/source/HTTP tests verify that configured protected requests still use the
shared ledger and current approval boundaries. All test adapters and tariffs are
synthetic; no provider requests occur.

Source checks and a candidate-image smoke test do not establish a live
installation. The private service cutover and the public Cloudflare restore
remain separate, revision-bound operational steps. In particular, the public
Worker currently stores account/session state durably, while workspace history,
memory, vectors and the new monetary ledger need their own coordinated durable
storage and restore evidence. An account-only cold start cannot close that gate.
