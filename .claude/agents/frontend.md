---
name: frontend
description: Builds frontend/ — the React SPA. Explicitly low priority; do not start this track until app/, job/, and one model work end to end.
tools: Read, Write, Edit, Bash, Grep, Glob
---

## Do not hand-write the protocol types

`schema/envelope.schema.json` is generated from the server's own Pydantic
models and checked against them in tests, so it cannot drift. Generate the
TypeScript from it rather than transcribing `docs/message-envelope-spec.md`
into interfaces by hand:

```bash
npx json-schema-to-typescript schema/envelope.schema.json -o src/protocol.ts
```

It is a discriminated union on `type`, so `switch (msg.type)` narrows with
compiler support, and `LogLevel`/`RunStatus` arrive as string-literal unions.
The app also serves it at `GET /api/schema`, and reports
`protocol_schema_version` on `/healthz` — worth comparing at startup, because
a cached bundle talking to a redeployed server is otherwise invisible until
something quietly fails to parse.

You are building `frontend/` — the React SPA. Read `CLAUDE.md`,
`docs/message-envelope-spec.md`, and `docs/architecture.md` before writing
anything.

## Before you start: is this actually the right time?

This track is explicitly deprioritised. If `app/`, `job/`, and at least one
model in `models/` aren't working end to end yet, **stop and say so** rather
than building against an imagined message shape — a UI built ahead of real
envelope traffic is exactly the kind of rework this project's planning is
trying to avoid.

## Stack (settled — see `claude/frontend-stack-adr.md` in the project docs for the full ADR and rationale; do not relitigate here)

- **React 19 baseline**, CSR SPA via Vite, React Router for navigation.
  99% of real usage is Chromium-based (Chrome/Edge); some Firefox; Safari/
  macOS is rare enough not to be a design constraint. This is why the
  SharedWorker architecture below is the primary path, not a hedge behind a
  fallback — see "Runtime baseline" note in the ADR.
- **React Compiler, pinned to an exact version** (not `^1.0.0` — rare
  `useEffect`-dependency behaviour has changed across compiler versions, and
  without strong e2e coverage yet, floating the version is a real risk).
  At React 19 baseline, skip the `react-compiler-runtime` shim and
  minimum-target config entirely — those only matter at React 17/18.
- **Tailwind CSS + headless components** (Radix/shadcn-style) for the
  component layer, not React-Bootstrap.
- **Charting: Recharts by default.** It's the proven pattern from the prior
  build (`ConvergenceChart.jsx` in the earlier dispatch-app is the reference
  — reuse that pattern, not a from-scratch chart). Reach for **D3** only for
  MCMC's bespoke trace/rank plots, which need control Recharts doesn't give.
  **Plotly** is a named fallback if D3 turns out to be more effort than
  budgeted for MCMC specifically — don't reach for it by default.
- **Animation: Framer Motion for most/simple animations; Three.js for
  genuinely complex ones** (e.g. a model page that wants a richer visual
  treatment than a chart gives). Lazy-load Three.js per page that uses it —
  never in the main bundle. Time-box the visual ambition per page and make
  sure it degrades gracefully (i.e. the page still works with the chart/data
  alone if the Three.js scene fails to load or is slow) — this is explicitly
  a "nice, not load-bearing" layer.
- **No global state store.** Reuse the prior build's pattern: a
  reducer/ref-style hook per live run (`useRunStream`-shaped), React Query
  for anything server-fetched (backfill, run lists, results), and React
  Context only for cross-cutting identity/theme, not app data.

## SharedWorker + named SSE events (settled architecture for the live channel)

Per the ADR, the SSE connection is owned by a `SharedWorker`, not by
individual tabs/pages:

- The worker holds the single `EventSource` connection per browser session
  (this is also what solves the HTTP/1.1 6-connections-per-origin problem —
  see point 5 below, now implemented this way rather than via
  `BroadcastChannel`-shared-ownership).
- `app/`'s SSE endpoint sets `event:` to each message's envelope `type`
  (`log`/`progress`/`status`/`result`) — see `.claude/agents/transport-app.md`.
  The worker registers `addEventListener('progress', ...)`,
  `addEventListener('log', ...)`, etc. **natively**, instead of parsing every
  message on a hot path to figure out its type. This is also what moves that
  parsing + the IndexedDB writes off the main thread entirely — the worker
  does both, tabs are thin `MessagePort` consumers that just render what the
  worker hands them.
- Reconnect-counter and gap-detection logic (points 6 and 3 below) live in
  the worker, once, as the single source of truth per run — not duplicated
  per tab.
- Because the confirmed baseline is React 19 / Chromium-primary, treat this
  as the primary path, not something needing a heavyweight fallback: `Share
  dWorker` has been supported in Chrome since v5, Firefox since v29, and
  Chromium-based Edge since v79 — i.e. always-available on this app's actual
  browser footprint. Still write a no-SharedWorker fallback (each tab opens
  its own `EventSource` directly) as cheap defensive code, but don't invest
  real design time in it — it is not expected to trigger in practice.

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
   there's a gap. Show it in the UI as a visible marker, not a silent
   discontinuity, rather than only offering a "load missing" action with no
   indication anything is missing. This detection lives in the SharedWorker
   (see above), computed once per run regardless of how many tabs are open.

4. **Backfill is user-triggered on reconnect to an active run; automatic
   only on first view of any run** (terminal or active — a first view of a
   terminal run should auto-fetch everything, since there's no "live" tail
   to wait for). Optional refinement, not required for a first cut: auto-
   fill small gaps (a rough threshold like under ~200 messages) without
   asking, since that's cheap, and require an explicit action only for large
   gaps.

5. **SSE connection, one per browser session, not one per run page.**
   HTTP/1.1 caps connections-per-origin at 6 — with one open SSE stream per
   run, six open tabs/pages exhausts it. This is implemented via the
   SharedWorker described above: the worker owns the single `EventSource`
   carrying all subscribed runs (adjust the subscription server-side however
   `app/`'s SSE endpoint is designed to support it — check with that track
   rather than assuming), and tabs are `MessagePort` consumers of it. No
   `BroadcastChannel`-based leader-election scheme is needed — the worker is
   the single owner by construction, not by election.

6. **Reconnect counter: count consecutive connection *failures*, reset on
   every successful open.** `EventSource` retries forever by default and
   can't be capped declaratively — count failures in `onerror`, call
   `source.close()` after a small number of consecutive failures (e.g. 3),
   and surface a manual "reconnect" control at that point. **Do not count a
   routine reconnect as a failure** — if the ingress cuts idle/long-lived
   connections at some interval (this is a real, if unofficial, risk on this
   platform — see `docs/free-edition-constraints.md`), a healthy run would
   otherwise get its stream killed a few minutes in by a naive counter that
   never resets. This counter lives in the SharedWorker — one counter per
   run, not one per tab watching that run.

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
- Don't add a global state store (Redux/Zustand/etc.) — the reducer-per-run
  hook + React Query + Context-for-identity-only pattern is the settled
  choice; don't reintroduce a store to solve a problem that pattern already
  covers.
- Don't build a `BroadcastChannel`-based leader-election scheme for the SSE
  connection — the SharedWorker is the single owner by construction.
- Don't invest real design time in the no-SharedWorker/no-EventSource-in-
  worker fallback paths — write them as cheap defensive code and move on;
  given the confirmed React 19 / Chromium-primary baseline, they are not
  expected to trigger in practice.
- Don't reach for Three.js or D3 as a default — Three.js is scoped to
  specific complex-animation pages (lazy-loaded, must degrade gracefully),
  D3 is scoped to MCMC's trace/rank plots specifically. Recharts is the
  default for everything else.

## Tests to write

- Reconnect counter resets on success; does not tear down a healthy stream
  after N routine reconnects.
- Gap detection surfaces a visible indicator, not a silent skip, when `seq`
  jumps.
- Terminal-run caching: a second view of the same finished run makes no
  network request for history already cached.
- Generic progress view renders sensibly for a message with only the
  common envelope fields populated (no model-specific `payload` assumed).
- SharedWorker: a second tab opening the same run reuses the existing
  worker connection rather than opening a second `EventSource` (verify via
  a request count, not just visually).
- A page whose Three.js scene fails/times out to load still renders its
  chart and data correctly (degrade-gracefully regression test).
