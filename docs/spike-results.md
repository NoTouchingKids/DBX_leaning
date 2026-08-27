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
  and `app/server/routes/runs.py` is reachable, not theoretical. HTTP push stays as
  the documented fallback rather than becoming the only tier.
- **SSE app→client works**, so `app/server/routes/stream.py` and `EventSource`'s
  native `Last-Event-ID` resume are the design, unchanged.

## Numbers still worth capturing

Confirmation that both *work* is the gate. Three measurements would still
change specific code, and none of them is recorded yet:

| Measurement | What it decides |
|---|---|
| Whether the ingress cuts a long-lived stream, and at what elapsed time | The frontend's reconnect-counter design. A counter that does not reset on success would kill a healthy stream within minutes if cuts happen every ~120s. That counter is now built and tested — `app/client/src/transport/hub.ts` counts *consecutive* failures, resets on every successful open, and gives up at 10 — so a real number no longer decides the design; it decides whether 10 is the right cap and whether the retry interval (`retry: 2000`, set in `app/server/routes/stream.py`) is sensible |
| Whether an *idle* connection is dropped sooner than an active one | `DBX_WS_PING_S` (default 20s) and the SSE keepalive (`DBX_SSE_KEEPALIVE_S`, default 10s). Both are currently set from community reports, not measurement |
| Whether SSE events are buffered or delivered promptly | Whether `X-Accel-Buffering: no` is doing anything here. If events arrive in held-and-released batches, live progress is not actually live |

Fill these in as they are observed — the probe commands
(`.claude/commands/spike-ws.md`, `.claude/commands/spike-sse.md`) describe how
to measure each one. Until then the defaults above are conservative guesses
that work rather than tuned values.

## The one auth question that goes with this

**Answered: it needs both, on different headers.**

`app/server/routes/ingest.py` checks a shared `DBX_APP_TOKEN`, which authenticates the
job *process* and skips entirely when none is configured (development
posture). That is not a Databricks identity, and the Apps proxy in front of
the app lets nothing through without one — so a job presenting the shared
secret in `Authorization` never reaches the app at all.

So the job presents an OAuth token for a service principal in
`Authorization`, and the shared secret in `X-DBX-App-Token`. `job/auth.py`
finds an identity from whatever the runtime offers — an explicit token,
client credentials for the same principal the app uses, a PAT, or the job's
own `dbutils` identity — and that principal needs `CAN_USE` on the app.
See "How a job reaches the app" in `deploy/README.md`.

A job with no identity runs unobserved rather than failing, which is the
same state as the app being down.
