# Core follow-up hardening

Base: main `f3a9c53146837eb1d2b8663da04ddeaf3ddc6b7a`. This change ports the
review fixes onto main without the separate private model-training, usage-ledger
or owner-pilot development branches. No production workflow or provider setting
is changed. A pull-request CI result is evidence about its exact tested revision,
not proof of a deployed runtime.

## Resulting behavior

- Calculator input accepts only bounded real numeric expressions. Strings,
  booleans, containers, complex/nonfinite values, oversized expressions/ASTs and
  excessive powers are rejected. Integer-power size is checked before allocation;
  intermediate numbers and function arity/rounding are bounded too.
- Text statistics compute whitespace outside the f-string, including Unicode
  whitespace. README declares Python 3.12 or newer, matching the existing CI and
  container baseline. The tool's Python-3.11 grammar check is not full 3.11 support.
- Invalid planner output produces an explicit non-executable parse state and
  fixed diagnostic reason, rather than invented replacement steps. Structural
  admission cannot be bypassed by a permissive critic. Bounded repairs may produce
  a valid plan; otherwise no specialist, synthesis or consolidation inference runs.
  Plan state/version remain visible in receipts, HTTP results, CLI JSON and history.
- Workspace runtimes use a bounded LRU cache and access-driven idle TTL. Active
  and queued request leases prevent eviction; overload returns 503/Retry-After.
  Closing waits for existing leases, tries all resources and retains failed
  cleanup for retry. Memory/history/vector files survive eviction and reopen.
- Password verification accepts only the canonical internally written fixed
  Scrypt format and buffer sizes. Malformed hashes cannot select arbitrary work
  factors or decoded buffer sizes; existing canonical passwords still verify.

SQLite constructors explicitly close connections on failed initialization. This
is required for reliable cache eviction/retry; it does not add a new storage or
usage-accounting subsystem.

## Validation

The main-based candidate passed 262 focused tests locally on Windows/Python 3.14.6,
covering calculator abuse, password-hash rejection before Scrypt, planner admission
and repair, actual CLI JSON/history export, cache lifecycle/concurrency, partial
initialization cleanup and the existing HTTP routes. Compilation passed. No real
provider or paid search request was used.

The complete existing Linux/Python-3.12 CI and Cloudflare/container checks must run
on the pull-request revision. Local focused success does not substitute for them.
The cache bounds the number of instances, not arbitrary total process memory;
TTL cleanup runs on access rather than in a new background scheduler.

The first Linux CI run passed 547 tests and the complete Cloudflare/container job,
but one regression expected the old mock rejection artifact. The plan gate now
stops after plan review (two calls), instead of continuing to five calls before
returning a different rejection. A separate observation file,
`evaluation/cooperative_acceptance_v1_candidate_plan_gate.json`, records this
provider-free measurement against source revision
`4603e5f7172b94685370d5b273b25ff6cd635a13`. The frozen corpus, baseline and historical
default candidate remain unchanged, including existing human-review bindings.
The new observation retains the same 0.1778 score, no demonstrated quality gain
and `pilot_eligible=false`. It is not a new successful benchmark or a live test.

Public paid-provider activation, monetary budget admission, production backup/
restore and a server release remain independent work with their existing gates.
