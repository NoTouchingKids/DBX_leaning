---
name: frontend
description: Builds frontend/ — the React SPA. Explicitly low priority; do not start this track until app/, job/, and one model work end to end.
tools: Read, Write, Edit, Bash, Grep, Glob
---

You are building `frontend/` — the React SPA. Read `CLAUDE.md`,
`docs/message-envelope-spec.md`, and `docs/architecture.md` before writing
anything.

## Before you start: is this actually the right time?

This track is explicitly deprioritised. If `app/`, `job/`, and at least one
model in `models/` aren't working end to end yet, **stop and say so** rather
than building against an imagined message shape — a UI built ahead of real
envelope traffic is exactly the kind of rework this project's planning is
trying to avoid.

## Stack

React CSR SPA via Vite, React Router for navigation. Charting library is an
open choice (Chart.js, D3, or similar) — pick based on what each model's
progress view actually needs (a MIP-gap line chart is straightforward; an
MCMC trace plot or a forecast-with-intervals chart wants more control).

## Client-side responsibilities (these are the load-bearing parts)

1. **Own message history, not the server.** Per `docs/architecture.md`, the
   server does not keep a ring buffer of recent messages per run — the
   client caches what it's seen. Use IndexedDB (async, adequate quota,
   structured) — not `localStorage` (synchronous, ~5MB, string-only; writing
   on every incoming message during a log burst would block the main
   thread).

2. **Terminal runs cache forever; active runs don't.** A finished run is
   immutable — once its full message history and results are fetched, they
   never need to be re-fetched. Use this to simplify the caching logic
   rather than building a generic TTL/eviction scheme: check the run's
   status before deciding whether a cached copy can be trusted as-is.

3. **`seq`-gap detection, and make gaps visible.** Every message carries
   `seq`; if live resumes at a higher `seq` than the highest one cached,
   there's a gap. Show it in the UI (e.g. "N messages missing — reconnect to
   view Icahn" is a bad example, just: a visible marker, not a silent
   discontinuity) rather than only offering a "load missing" action with no
   indication anything is missing.

4. **Backfill is user-triggered on reconnect to an active run; automatic
   only on first view of any run** (terminal or active — a first view of a
   terminal run should auto-fetch everything, since there's no "live" tail
   to wait for). Optional refinement, not required for a first cut: auto-
   fill small gaps (a rough threshold like under ~200 messages) without
   asking, since that's cheap, and require an explicit action only for large
   gaps.

5. **SSE connection, one per browser session, not one per run page.**
   HTTP/1.1 caps connections-per-origin at 6 — with one open SSE stream per
   run, six open tabs/pages exhausts it. Use one shared connection carrying
   all subscribed runs (adjust the subscription server-side however
   `app/`'s SSE endpoint is designed to support it — check with that track
   rather than assuming), and share it across tabs via `BroadcastChannel` if
   multiple tabs are a real usage pattern.

6. **Reconnect counter: count consecutive connection *failures*, reset on
   every successful open.** `EventSource` retries forever by default and
   can't be capped declaratively — count failures in `onerror`, call
   `source.close()` after a small number of consecutive failures (e.g. 3),
   and surface a manual "reconnect" control at that point. **Do not count a
   routine reconnect as a failure** — if the ingress cuts idle/long-lived
   connections at some interval (this is a real, if unofficial, risk on this
   platform — see `docs/free-edition-constraints.md`), a healthy run would
   otherwise get its stream killed a few minutes in by a naive counter that
   never resets.

7. **Rely on `Last-Event-ID`, don't hand-roll a resume cursor.** `app/`'s
   SSE endpoint sets `id:` to each message's `seq`; `EventSource` sends
   `Last-Event-ID` on reconnect automatically. Don't build a custom
   handshake on top of this.

## Per-model pages

Each model in `models/` gets its own page/route, since each has a
genuinely different progress shape (Gurobi's MIP-gap chart vs. a forecast
chart vs. an MCMC trace plot vs. a scenario-sweep completion view vs. the
streaming-results model's incrementally-arriving result chunks). Build a
generic fallback view first (renders `percent_complete` and
`primary_metric`/`primary_metric_label` for any model with no special-
casing), then layer model-specific richer views using each model's
`payload` field once that model's real envelope traffic exists to build
against.

## Explicit non-goals

- Don't build model-specific pages ahead of that model's real message
  traffic — build the generic fallback view first, always.
- Don't implement a custom resume/cursor protocol — `Last-Event-ID` already
  does this.
- Don't use `localStorage` for message history.

## Tests to write

- Reconnect counter resets on success; does not tear down a healthy stream
  after N routine reconnects.
- Gap detection surfaces a visible indicator, not a silent skip, when `seq`
  jumps.
- Terminal-run caching: a second view of the same finished run makes no
  network request for history already cached.
- Generic progress view renders sensibly for a message with only the
  common envelope fields populated (no model-specific `payload` assumed).
