# Ingress probe results

Both probes gate the build-out (`CLAUDE.md`, "How to work in this repo").
`/orient` checks this file to know whether that gate has been cleared.

> **Status: CLEARED — WebSocket and SSE both work through the Databricks Apps
> ingress**, confirmed by the project owner on 2026-08-23 against a real
> workspace.
>
> This is the question that stayed open across all three builds of this
> platform — v1 avoided WebSockets on the strength of it, v2 shipped
> feature-complete with "verify the WebSocket survives the ingress" as
> unfinished item #1 and was never deployed. It is now answered.

## What this unblocks

The transport in `docs/architecture.md` is the one being built, not a
best-effort guess:

- **WebSocket job→app works**, so it is the preferred live channel and
  **cancel has a real path** — the inbound command in `shared/protocol.py`
  and `app/routes/runs.py` is reachable, not theoretical. HTTP push stays as
  the documented fallback rather than becoming the only tier.
- **SSE app→client works**, so `app/routes/stream.py` and `EventSource`'s
  native `Last-Event-ID` resume are the design, unchanged.

## Numbers still worth capturing

Confirmation that both *work* is the gate. Three measurements would still
change specific code, and none of them is recorded yet:

| Measurement | What it decides |
|---|---|
| Whether the ingress cuts a long-lived stream, and at what elapsed time | The frontend's reconnect-counter design. A counter that does not reset on success would kill a healthy stream within minutes if cuts happen every ~120s. That counter is now built and tested — `frontend/src/transport/hub.ts` counts *consecutive* failures, resets on every successful open, and gives up at 10 — so a real number no longer decides the design; it decides whether 10 is the right cap and whether the retry interval (`retry: 2000`, set in `app/routes/stream.py`) is sensible |
| Whether an *idle* connection is dropped sooner than an active one | `DBX_WS_PING_S` (default 20s) and the SSE keepalive (`DBX_SSE_KEEPALIVE_S`, default 10s). Both are currently set from community reports, not measurement |
| Whether SSE events are buffered or delivered promptly | Whether `X-Accel-Buffering: no` is doing anything here. If events arrive in held-and-released batches, live progress is not actually live |

Fill these in as they are observed — the probe commands
(`.claude/commands/spike-ws.md`, `.claude/commands/spike-sse.md`) describe how
to measure each one. Until then the defaults above are conservative guesses
that work rather than tuned values.

## The one auth question that goes with this

A job reaching a Databricks App needs a credential the app accepts on the
handshake. `app/routes/ingest.py` checks a shared `DBX_APP_TOKEN` on both the
WS and HTTP-push ingress, and skips the check entirely when none is
configured (development posture). Whether that is the right mechanism — or
whether the job should present an OAuth token for its service principal —
is a deployment decision, not an ingress one, and belongs with the bundle
config rather than here.
