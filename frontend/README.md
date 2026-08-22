# frontend/ — not started, on purpose

Explicitly low priority until `app/`, `job/` and one model work end to end
(`CLAUDE.md`, "How to work in this repo"; `.claude/agents/frontend.md`).

That bar is now met offline — the vertical slice runs and is tested — but the
thing this track most needs is still missing: **real envelope traffic from a
deployed run**. A UI built against an imagined message shape is exactly the
rework `docs/parallelization-plan.md` freezes `shared/` to avoid.

## What to read first when this does start

- `docs/message-envelope-spec.md` — the four message types, in full
- `app/routes/stream.py` — the SSE contract: `id:` is the message's `seq`, so
  `EventSource`'s own `Last-Event-ID` resume works with no custom handshake
- `app/routes/runs.py` — backfill (`GET /api/runs/{id}/messages`) and cancel
- `docs/spike-results.md` — specifically the observed SSE cut interval

## The trap that is already known

The reconnect counter must count **consecutive** failures and reset to zero on
every successful reconnect. A naive "give up after 3 tries" would kill a
perfectly healthy stream a few minutes in if the ingress cuts connections
periodically — which community reports put at ~120s. That is not hypothetical
here; it is the specific number `/spike-sse` exists to measure, and the reason
this file mentions it before any code is written.
