# Ingress probe results

Both probes gate the build-out (`CLAUDE.md`, "How to work in this repo").
`/orient` checks this file to know whether that gate has been cleared.

> **Status: NEITHER PROBE HAS RUN.**
> They cannot run from a development container — the behaviour they measure
> exists only on the deployed Databricks Apps ingress. Everything in `app/`
> and `job/` is written against both outcomes (WebSocket with an HTTP-push
> fallback; SSE with `EventSource`'s own reconnect), but the *numbers* below
> are what turn "designed for" into "known".

## WS probe

**Not run.** See `.claude/commands/spike-ws.md`.

| Question | Result |
|---|---|
| Does the `Upgrade` handshake succeed at all? | — |
| Idle pass: survives 10 min on app-level pings only? | — |
| Active pass: survives 10 min with traffic every ~5s? | — |
| If dropped: at what elapsed time, and does reconnect succeed? | — |

**Add a third question when this runs:** *auth*. A job reaching a Databricks
App needs an OAuth token for a service principal, and the app has to accept it
on the handshake (`app/routes/ingest.py` checks `DBX_APP_TOKEN`). Run one
unauthenticated attempt and one authenticated attempt and report them
separately — otherwise a failure cannot be told apart from an ingress refusal.

## SSE probe

**Not run.** See `.claude/commands/spike-sse.md`.

| Question | Result |
|---|---|
| Do events arrive promptly, or in held-and-released batches (buffering)? | — |
| Does `X-Accel-Buffering: no` change anything here? | — |
| Does the stream survive 10 minutes, or get cut? At what elapsed time? | — |
| Does `EventSource` reconnect automatically, and is `Last-Event-ID` sent? | — |

The observed cut interval, if there is one, is the number the frontend track
needs: a "stop after 3 consecutive failures" counter that does not reset on
success would kill a healthy stream in ~6 minutes if cuts happen every ~120s.
Record the interval here specifically so that can be tested against a real
number rather than a guess.

## What already works without either answer

`shared/`, `job/`, `app/` and all five models are built and tested offline
(`pytest`). What the probes decide is not *whether* the code works, but which
live path it will actually be using in production:

- WS refused entirely → HTTP push becomes the only live channel, and cancel
  has no path at all (the escape hatch is `databricks jobs cancel-run`, which
  `app/routes/runs.py` already returns as its 409 detail).
- SSE cut periodically → no code change; confirm the frontend's reconnect
  counter resets on success.
