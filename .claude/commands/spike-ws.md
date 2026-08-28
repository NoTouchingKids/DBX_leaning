Build and run a throwaway probe to answer: **does the Databricks Apps
ingress pass a WebSocket `Upgrade` handshake at all, and does it hold an
idle connection open, or drop it after some interval?**

Read `docs/free-edition-constraints.md` ("Databricks Apps ingress —
unresolved") first for the specific numbers other builds have reported
(~30s idle drop, ~120s stream cut) — this probe exists to confirm or refute
those against this actual workspace, not to take them on faith.

This is throwaway code — put it in a `spikes/ws/` directory, not under
`app/` or `job/`. Do not try to make it production-shaped.

## What to build

1. **A minimal FastAPI (or plain ASGI) app with one WebSocket endpoint**,
   deployed as its own tiny Databricks App (or added temporarily to
   whatever app deployment already exists, if that's faster to iterate on —
   your call, note which you did). The endpoint should:
   - Accept the connection
   - On receiving any text frame, echo it back with a server timestamp
   - Send an app-level ping frame (a small JSON message, not a WS protocol
     ping — those can be swallowed by some proxies without reaching your
     handler) every ~20 seconds so you can distinguish "the ingress dropped
     this" from "nothing was sent for a while."

2. **A connecting script** — run it from `job/`'s eventual position (i.e. a
   plain Python script using `websockets` or similar, connecting as the
   job's service principal / whatever auth context a real Databricks Job
   would have) that:
   - Connects, then sits idle (sends nothing) for at least **10 minutes**,
     logging every received ping and any disconnect with a timestamp
   - Separately, run a second pass where the client also sends a message
     every ~5 seconds instead of sitting idle, again for 10+ minutes, to
     distinguish an *idle* timeout from a *duration* cap
   - Log reconnection attempts and whether they succeed, if the connection
     does drop

3. **Run both passes against the actual deployed Databricks App**, not
   locally — the ingress behaviour being tested only exists on the real
   platform. Local `docker compose`/`uvicorn` testing tells you nothing
   about this specific question.

## What to report back

State plainly, with the actual observed numbers, not a guess:

- Did the `Upgrade` handshake succeed at all?
- Idle pass: did the connection survive 10 minutes with only app-level
  pings? If not, at what elapsed time did it drop?
- Active pass: did the connection survive 10 minutes with regular client
  traffic? If not, at what elapsed time did it drop, and does that number
  look like a duration cap rather than an idle timeout?
- If it dropped in either pass: did a reconnect attempt succeed?

Write the result to `docs/spike-results.md` (create it if it doesn't
exist) under a `## WS probe` heading, with the date and the numbers
observed — this is what future sessions (including `/orient`) check for to
know whether this gate has been cleared.

## If it fails

Don't try to fix the ingress — that's not something this app controls.
Report the failure mode clearly (upgrade rejected outright, vs. connects but
drops at N seconds) and note it in `docs/spike-results.md`.

Be aware before you start: **this probe has already passed against a real
workspace**, and the HTTP-push fallback earlier versions of this file pointed
at has since been removed from `job/`. The socket is now the only live
channel there is, so a failure here is not "fall back to the other tier" — it
is a run going unobserved, which the durable path already tolerates. Re-run
this to measure timings, not to decide the architecture.
