# Public registration

The production configuration enables `TANKAI_ALLOW_REGISTRATION`. The Worker
passes the flag to the container, and the chat page offers registration only when
the health endpoint reports it enabled. Existing users can still sign in.

Registration is limited through the existing auth-state Durable Object: three
attempts per client per UTC hour, and fifty successful or unresolved registrations
per UTC day. Client identifiers are truncated 128-bit HMACs; raw client IPs are
not stored by this limiter. Hourly shards have bounded capacity and may reject new
clients when full. Explicit application rejections release their daily reservation;
successful responses and uncertain failures retain it. Limits survive restarts.

The existing auth snapshot transport and its 8 MiB cap remain in use. This is a
bounded initial release, not an unlimited account service. There is currently no
email ownership verification. Keep these constraints visible in release review.

Validation includes Python HTTP account creation and subsequent login, actual
Worker forwarding with a controlled backend, persistent limiter tests, and UI
handler tests with a synthetic DOM. A real-browser/public deployment test remains
necessary before claiming the public user journey is verified.
