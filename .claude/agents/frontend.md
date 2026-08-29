---
name: frontend
description: Works on app/client/ — the React SPA. The transport spine is built and tested; the app shell is in progress. Read app/client/README.md first — it is ahead of this brief.
tools: Read, Write, Edit, Bash, Grep, Glob
---

## Status: this track has started, and this brief is behind the code

This file was written before any of `app/client/` existed. It is kept because
the *reasoning* in it is still the settled architecture and is not recorded
anywhere else. It is not a description of what is left to do.

**Read `app/client/README.md` first.** It is maintained alongside the code and
is the authority on what exists; this brief is the authority on why. Where
they disagree, the README and the source win. Concretely, since this was
written: the transport spine under `src/transport/` is finished and tested
(SharedWorker, IndexedDB, reconnect policy, gap detection), there is a
Playwright suite under `e2e/`, and the per-model pages are underway.

The gate below has been met — `app/`, `job/` and ten models work end to
end offline, and `tests/integration/test_end_to_end.py` drives real models
through the real harness. What is still missing is envelope traffic from a
**deployed** run. The bundle deploys; this client has not been driven
against telemetry from a deployed run.

## The wire contract: hand-written, with a drift test — not generated

This section used to say to generate the types with
`npx json-schema-to-typescript schema/envelope.schema.json -o src/protocol.ts`.
**Do not do that.** Two things changed:

- The generated output was unreadable — `RunId1`, `Seq1`, `Type1`, one alias
  per property occurrence — and carried none of the reasoning that makes the
  contract usable. `src/lib/envelope.ts` is hand-written instead, and
  `src/lib/envelope.contract.test.ts` is what stops it drifting: it checks
  **both** directions against `schema/envelope.schema.json` (every property
  and enum member the server can emit is declared here; nothing declared here
  is absent from the server, validated with ajv under the schema's own
  `additionalProperties: false`). That is a stronger guarantee than
  generation, because it also covers the prose.
- **`src/transport/protocol.ts` is now a different contract entirely** — the
  page↔worker protocol, `WorkerRequest`/`WorkerEvent`, about connections and
  seq holes rather than about run output. Writing the generated envelope to
  that path would overwrite it. Conflating the two is how a UI ends up
  rendering "reconnecting" as if it were a run state.

Still true and still worth doing: the app serves the schema at
`GET /api/schema` and reports `protocol_schema_version` on `/healthz` —
worth comparing at startup, because a cached bundle talking to a redeployed
server is otherwise invisible until something quietly fails to parse.

Read `CLAUDE.md`, `docs/message-envelope-spec.md`, and
`docs/architecture.md` before writing anything.

## Stack (settled — do not relitigate here)

The ADR this section originally cited (`claude/frontend-stack-adr.md`) is not
in this repo and never was — every reference to "the ADR" below is a dead
link. What survived of it is this list, plus the three constraints in
`app/client/README.md` ("The stack, and the one thing to know about each
choice"), which is the current record.

Three items below did not survive contact with the build, and
`app/client/package.json` is the authority on all of them:

- **No headless component library.** Radix/shadcn was never installed; the
  component layer is plain Tailwind v4. Adding one now is a real decision,
  not the settled default this list implies.
- **No D3, no Plotly, no Three.js.** Recharts does all of it, MCMC included.
  Treat the D3/Plotly/Three.js paragraphs below as options that were
  considered and not taken, not as a plan to execute.
- **React Compiler is NOT pinned.** `babel-plugin-react-compiler` is
  `^1.0.0`, which is the exact thing the next bullet says not to do. Either
  pin it or drop the instruction — but do not leave the file claiming a
  guarantee the manifest does not give.

- **React 19 baseline**, CSR SPA via Vite, React Router for navigation.
  99% of real usage is Chromium-based (Chrome/Edge); some Firefox; Safari/
  macOS is rare enough not to be a design constraint. This is why the
  SharedWorker architecture below is the primary path, not a hedge behind a
  fallback.
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
- **Animation: Motion (the package formerly published as Framer Motion) for
  most/simple animations; Three.js for genuinely complex ones** (e.g. a model page that wants a richer visual
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

The SSE connection is owned by a `SharedWorker`, not by individual
tabs/pages. This is the one section of this brief that was built exactly as
written — `src/transport/` is the implementation and
`app/client/README.md` ("The transport spine") is the map of it:

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

3. **`seq`-gap detection, and make gaps visible — but never act on a gap
   automatically.** Every message carries `seq`; if live resumes at a higher
   `seq` than the highest one cached, there is a hole. Show it as a visible
   marker, not a silent discontinuity. This detection lives in the
   SharedWorker, computed once per run regardless of how many tabs are open.

   **The correction this brief originally got wrong:** a gap is not
   necessarily a fault, and some holes never close. `seq` is gap-free at the
   source, but the live path never sends `client_visible=false` logs and the
   backfill endpoint filters them out too — so a run can be complete and
   correct and still show permanent holes. "Backfill until contiguous" is
   therefore an infinite loop, not a refinement. Report gaps; let a person
   decide.

4. **Backfill is user-triggered on reconnect to an active run; automatic
   only on first view of any run** (terminal or active — a first view of a
   terminal run should auto-fetch everything, since there's no "live" tail
   to wait for). This brief used to suggest auto-filling "small" gaps without
   asking; do not, for the reason in point 3 — a hole that cannot be filled
   would be retried forever.

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
   `source.close()` after a cap is hit, and surface a manual "reconnect"
   control at that point. Built: `hub.ts` owns it and the cap is **10**, not
   the "e.g. 3" this brief originally suggested — deliberately generous,
   because tripping it on a healthy stream is the expensive mistake and
   `useReconnectableRunStream` is the way back out once it does trip. **Do not count a
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

Each model in `job/models/` gets its own page/route, since each has a
genuinely different progress shape (Gurobi's MIP-gap chart vs. a forecast
chart vs. an MCMC trace plot vs. a scenario-sweep completion view vs.
`panel_fit`'s incrementally-arriving result chunks). Build a generic
fallback view first (renders `percent_complete` and
`primary_metric`/`primary_metric_label` for any model with no special-
casing), then layer model-specific richer views using each model's
`payload` field once that model's real envelope traffic exists to build
against.

The per-model contract this needs is `src/lib/models.ts`, and it is worth
knowing what it is before touching it: a **hand-derived** `ModelSpec` per
model, read out of every `cfg.get(...)` call and every `emit("progress", ...)`
payload in `job/models/<name>/model.py`. The server validates
`TriggerRequest.config` not at all — it is `dict[str, Any]` passed verbatim
into `DBX_MODEL_CONFIG` — so there is no schema to generate from and no test
that will tell you when this file falls behind. Re-derive it whenever
`job/models/` changes. It currently covers **all ten models**, and so does
`src/components/models/registry.ts` — so the drift to watch for is a model
gaining a config field or a payload key, not a model with no entry at all.

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
  default for everything else. In the end neither was needed: nothing in
  `package.json` pulls D3, Plotly or Three.js, and adding one is a new
  decision rather than executing this plan.
- Don't add a polling interval anywhere. The React Query client sets
  `refetchInterval: false` globally on purpose — every HTTP read here
  ultimately reaches a SQL warehouse billed by *uptime*, so a background
  interval on an open tab costs money all day for information the SSE stream
  already delivers. This is the rule most easily broken by accident, and it
  is not in the original list because it was learned later.

## Tests

These were written as a wishlist and are now mostly real — `src/**/*.test.ts`
in jsdom, plus six Playwright specs in `e2e/`. Check `app/client/README.md`
("Browser tests") before adding another: the jsdom transport tests run against
a fake `EventSource` and by construction cannot reproduce a real socket, a
real `SharedWorker`, an IndexedDB surviving navigation, or a reconnect —
which is the shape of every transport bug this project has actually shipped.
That is what `e2e/` is for, and it is not a place to re-run unit tests slowly.

The list this brief started with, kept because it is still the right list:

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
  chart and data correctly (degrade-gracefully regression test) — moot while
  no page uses Three.js.
