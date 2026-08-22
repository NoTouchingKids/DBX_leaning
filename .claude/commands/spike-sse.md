Build and run a throwaway probe to answer: **does the Databricks Apps
ingress buffer or cut a server-sent-events stream, and if it cuts one, after
how long — and does the buffering, if any, delay delivery of events that
were sent promptly?**

Read `docs/free-edition-constraints.md` ("Databricks Apps ingress —
unresolved") first — this probe exists to get real numbers for this
workspace, not to confirm what other builds have informally reported.

This is throwaway code — put it in `spikes/sse/`, not under `app/`.

## What to build

1. **A minimal FastAPI endpoint returning a `StreamingResponse` with
   `media_type="text/event-stream"`**, deployed to an actual Databricks App
   (reuse the one from `/spike-ws` if convenient, or a fresh throwaway one —
   your call). The endpoint should:
   - Send an event every 2 seconds, each with an incrementing `id:` field
     (this is what `Last-Event-ID` resume depends on later — confirm the
     mechanics work even in this throwaway version)
   - Include a comment-only keepalive line (`: keepalive\n\n`) if more than
     ~10 seconds pass with nothing else to send, so idle-timeout and
     duration-cap behaviour can be told apart the same way as the WS probe
   - Set `Cache-Control: no-cache` and `X-Accel-Buffering: no` headers (the
     latter specifically defeats buffering on some reverse proxies — test
     whether it has any effect here, since that's directly relevant to
     whether Databricks Apps' ingress buffers SSE at all)

2. **A connecting script or a simple browser page using `EventSource`** that:
   - Connects and logs every event received, with the local timestamp of
     receipt (not just the server's `id`/payload) — the gap between when
     the server actually sent something and when the client actually
     received it is what reveals buffering
   - Stays connected for at least **10 minutes**, logging any `onerror` /
     disconnect and whether `EventSource`'s automatic reconnect succeeds
   - Separately, check whether `Last-Event-ID` is actually sent correctly
     on an automatic reconnect — force a disconnect (kill the server-side
     connection, or just watch what happens if the ingress cuts it) and
     confirm the header shows up server-side on the next connection

3. **Run this against the actual deployed app**, not locally — same
   reasoning as the WS probe.

## What to report back

- Did events arrive with a receipt-timestamp gap consistent with "sent
  promptly, arrived promptly" (no buffering), or a pattern suggesting events
  are held and released in a batch (buffering)?
- Did the stream survive 10 minutes, or get cut? If cut, at what elapsed
  time, and does `EventSource` reconnect automatically at that point?
- Did `Last-Event-ID` show up correctly on an automatic reconnect?

Write the result to `docs/spike-results.md` (create it if it doesn't exist,
alongside the WS probe's section) under a `## SSE probe` heading, with the
date and the numbers observed.

## If it cuts around ~120 seconds (or any duration under normal browser tab
lifetimes)

This isn't a failure requiring a different transport — SSE with
`EventSource`'s built-in reconnect already handles periodic cuts
transparently, per `docs/architecture.md`. What it does change: confirm the
reconnect-counter design in `frontend/` (see `.claude/agents/frontend.md`)
actually needs to tolerate reconnects at that specific interval — i.e. if
cuts happen every ~120s, a "stop after 3 consecutive failures" counter that
doesn't reset on success would kill a healthy stream in ~6 minutes, and this
probe result is the concrete number that makes that risk real rather than
hypothetical. Note the observed interval in `docs/spike-results.md`
specifically so the frontend track can test against it.
