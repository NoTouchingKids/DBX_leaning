# frontend/ — the React SPA

The transport spine is built and tested. The app shell around it is being
written. This file is the entry point for anyone joining the track, and it is
deliberately a map rather than a manual — the reasoning lives in the source
files' own headers, which are long on purpose, and restating it here is how
two versions of it end up disagreeing.

The bar this track was gated on (`CLAUDE.md`, "How to work in this repo";
`.claude/agents/frontend.md`) was `app/`, `job/` and one model working end to
end. That is met: `shared/`, `job/`, `app/` and nine models are built and
tested, `tests/integration/test_end_to_end.py` drives real models through the
real harness offline, and the ingress probes cleared
(`docs/spike-results.md`). What is *still* missing is envelope traffic from a
**deployed** run — `databricks bundle deploy` has never been executed against
a workspace — so everything here is built against locally-run models, the
generated schema, and tests.

## The stack, and the one thing to know about each choice

pnpm, Vite, React 19, TypeScript, React Router, TanStack Query, Tailwind v4,
Recharts, Motion. Vitest + Testing Library + jsdom for tests, oxlint for lint.

Three of those carry a constraint rather than a preference:

- **It is a client-rendered SPA, and cannot be anything else.** Databricks
  Apps has no Node runtime, so nothing in the workspace will ever render or
  build this. `app/spa.py` serves the built bundle: it mounts `dist/assets`
  at `/assets` and returns `index.html` for any non-API path. That is why
  `vite.config.ts` pins `base: "/"` and `assetsDir: "assets"`.
- **React Compiler runs on the stable Babel path**, not plugin-react v6's
  faster experimental Rust one. It changes the semantics of every component
  in the app; that is not the place to run something labelled experimental.
- **Nothing polls.** The React Query client sets `refetchInterval: false`
  globally and relies on refetch-on-focus. Every HTTP read here ultimately
  reaches a SQL warehouse billed by *uptime*, so a background interval on an
  open tab costs money all day for information the SSE stream already
  delivers. This is the same rule the backend follows, and it is the single
  easiest one to break by accident.

## Running it

```bash
cd frontend
pnpm install
pnpm dev          # :5173, proxying /api, /ws and /healthz to :8000
pnpm test         # vitest, once
pnpm lint         # oxlint
pnpm typecheck    # tsc -b --force
pnpm build        # tsc -b, then vite build -> dist/
```

Run the API alongside it — `uv run uvicorn app.main:app --reload` from the
repo root. The dev proxy points at a real FastAPI app deliberately: named SSE
events, `Last-Event-ID` resume and a genuine connection cut are not things a
mock reproduces usefully.

`/dev/probe` is a route in the app itself (`src/dev/StreamProbe.tsx`) for
watching a run's raw transport state — connection tier, connection state, seq
gaps — without the rest of the UI in the way.

**`pnpm build` is a required step before `databricks bundle deploy`, not an
optional one.** The bundle syncs `frontend/dist` and excludes the source;
skip the build and the deploy succeeds, the API works, and every page answers
503 with `app/spa.py`'s `NO_BUNDLE` message. See `deploy/README.md`.

## Browser tests (`e2e/`)

```bash
pnpm e2e                      # playwright test, Chromium
pnpm exec playwright test e2e/01-live-run.e2e.ts     # one file
DBX_E2E_APP_PORT=9200 pnpm e2e                       # if 8811/8812 are taken
```

Six tests, and they are the only ones here that run against a **real browser
talking to a real server**. The transport tests under `src/` run in jsdom
against a fake `EventSource` — which by construction cannot reproduce SSE over
a real socket, a `SharedWorker`, an IndexedDB that survives a navigation, or a
reconnect. Every transport bug this project has actually shipped was of that
shape: correct in every offline test, wrong on first contact with a real
browser. This suite exists for that class and no other; it is not a place to
re-run the unit tests slowly.

`pnpm e2e` needs nothing running first. Global setup builds the SPA, starts
`scripts/dev_stack.py` (real `app/` under uvicorn, real `job/` harness per
run, real embedded Postgres behind the real `PostgresRunStore`, real models —
only the Databricks Jobs API is substituted) and waits for `/healthz`. The app
serves the built bundle itself, the way `app/spa.py` does in a deploy, so
`/api`, `/ws` and the bundle share one origin and the dev proxy is not
involved. **If the stack cannot start, setup throws with the stack's own
output.** Nothing is mocked and nothing is skipped to keep a run green. It
needs `uv` and the repo's Python extras (`uv sync --all-extras`), and a
Chromium for Playwright — `pnpm exec playwright install chromium` if
`PLAYWRIGHT_BROWSERS_PATH` does not already point at one.

Nothing is written inside the repo — the build, Postgres, the stack log and
Playwright's traces all live under `/tmp/dbx-leaning-e2e` (`DBX_E2E_WORK_DIR`).

What is covered, and how each one is actually observed:

| File | Asserts |
| --- | --- |
| `01-live-run` | A run triggered from the real form streams `percent_complete`, logs, a terminal status and a result count into the DOM — over SSE, since the local stack has no warehouse and the backfill endpoints answer 503. Cross-checked against the durable JSONL the writer produced in parallel. |
| `02-shared-worker` | Two tabs in one profile cause **one** `GET .../stream`; a second profile causes a second. The control is the point: it shows the instrument detects an extra connection when there is one. |
| `03-terminal-run` | A finished run opens no live channel — passing when the client has it cached, **failing when it does not**. See below. |
| `04-reload-mid-run` | A reload mid-run keeps the history the tab already had, renders nothing twice, and keeps following the run to completion. |
| `05-concurrency-429` | Filling the account-wide ceiling of five makes the next trigger render the server's own 429 text, counts and all. Real `PostgresRunStore` count-and-claim. |

Two things to know before writing another one:

- **Files are `*.e2e.ts`, not `*.spec.ts`.** Vitest's default `include` claims
  `*.{test,spec}.ts` anywhere under this directory and `vite.config.ts` sets
  no `include` of its own, so a Playwright file named `*.spec.ts` would be
  collected by `pnpm test` and fail in jsdom.
- **The SPA's `EventSource` lives in a `SharedWorker`, which Playwright cannot
  see.** `page.on("request")` never fires for the stream path — a SharedWorker
  is a separate browser target with no page association. Connection counts are
  therefore read from the app's own access log; `e2e/stack.ts` explains the
  method and its one caveat.

**One test is expected to fail, and says so with `test.fail()`.** Opening a
finished run in a browser that has never seen it opens one SSE connection to
it, contradicting "a terminal run gets no live channel" — behaviour 3 of the
transport spine, below. `RunWorkspace` subscribes before
`GET /api/runs/{id}` has resolved, so `terminal` is still `false`, and
`useRunStream` reads it once and deliberately keeps it out of the dependency
list. The warm-cache path escapes it only because `hub.ts` finds
`cached.terminal` in IndexedDB first. The channel closes as soon as the
server's connect-time snapshot arrives, so the cost is one wasted connection
per cold view — small, and exactly what the rule exists to prevent. Deleting
the `test.fail()` is the acceptance check for a fix.

## The transport spine

This is the part that is finished, and the part worth reading before writing
anything that touches a run.

```
src/transport/
  hub.ts         StreamHub — one EventSource per watched non-terminal run.
                 Parse, gap detection, persistence, reconnect policy.
  worker.ts      A thirty-line shell that hands StreamHub a real MessagePort
                 and a real IndexedDB. Body of both the SharedWorker and the
                 dedicated-Worker fallback.
  client.ts      Page side. One worker per tab, subscriptions ref-counted
                 per run, three tiers: SharedWorker -> Worker -> in-page.
  db.ts          IndexedDB, hand-rolled. `messages` keyed [run_id, seq].
  runStore.ts    Per-run state, split by message type, bounded, drops counted.
  normalize.ts   The ONE normaliser, for live frames and backfilled rows alike.
  protocol.ts    The page<->worker protocol. NOT the server envelope.
  useRunStream.ts  useSyncExternalStore. The only thing a component needs.
```

The shape of it, in one paragraph: a `SharedWorker` owns exactly one
`EventSource` per non-terminal run across *all* tabs, because on Free Edition
connections are the scarce thing and five tabs on one run must not be five
connections. It parses frames off the main thread, writes them to IndexedDB
keyed `[run_id, seq]` — which makes dedupe free, since a live message and its
backfilled twin collapse on `put`, and is the whole reason `seq` is assigned
by the job rather than by a UC identity column — and posts them to each
subscribed tab. A component calls `useRunStream(runId)` and gets a snapshot
already split into logs, progress, statuses and results.

Four behaviours in there are load-bearing, and each is pinned by a test:

1. **The reconnect counter counts *consecutive* failures and resets to zero
   on every successful open.** This used to be the one section of this README
   worth keeping; it is now code. `hub.ts` owns it (the cap is 10), and
   `hub.test.ts` is what stops it regressing — read those rather than a
   prose restatement. The trap it avoids: a counter that only ever increments
   kills a perfectly healthy stream a few minutes in if the ingress cuts
   connections periodically, and the symptom is indistinguishable from the
   server dying. `useReconnectableRunStream` adds the way back out once the
   cap *is* hit, by releasing and re-acquiring the subscription.
2. **A gap is not necessarily a fault, and may never close.** `seq` is
   gap-free at the source, but the live path never sends
   `client_visible=false` logs and the backfill endpoint filters them out
   too. Some holes are permanent by design. Gaps are reported and never acted
   on automatically; "backfill until contiguous" is an infinite loop.
3. **A terminal run gets no live channel.** Nothing further will ever arrive,
   so an `EventSource` on it is a connection that exists only to be cut and
   retried.
4. **The production build must emit a separate worker chunk.**
   `worker-bundle.test.ts` runs the real Vite build and asserts it. Vite
   only recognises a worker entry when `new URL("./worker.ts",
   import.meta.url)` is written *inside* the `new SharedWorker(...)` call;
   hoist it into a helper and everything still compiles, dev still works, and
   production silently emits no worker chunk and falls through to the in-page
   tier. It has already happened once.

## The wire contract

`src/lib/envelope.ts` is the TypeScript view of `shared/envelope.py`, and it
is **hand-written on purpose**. `json-schema-to-typescript` produced output
nobody could read (`RunId1`, `Seq1`, `Type1`, one alias per property
occurrence) carrying none of the reasoning that makes the contract usable.

The cost of hand-writing it is that it can silently fall behind the server.
`src/lib/envelope.contract.test.ts` is what stops that, and it checks both
directions against `schema/envelope.schema.json` — which is generated from
the Pydantic models by `scripts/export_schema.py`, so it cannot itself drift.
Every property and enum member the server can emit must be declared here, and
nothing declared here may be absent from the server or fail to validate.

If you ever do decide to generate instead: **do not call the output
`protocol.ts`.** `src/transport/protocol.ts` already owns that name and is a
different thing — a `Message` is what a run emits, a `WorkerEvent` is what
the worker says about the *transport*. Conflating the two is how a UI ends up
rendering "reconnecting" as if it were a run state.

The app also serves the schema at `GET /api/schema` and reports
`protocol_schema_version` on `/healthz`, so a cached bundle and a redeployed
server can notice they disagree.

## Per-model views

`src/components/models/contract.ts` is frozen, and was frozen deliberately
before the nine views were fanned out — same reason `shared/` is frozen on
the Python side. Read its header before writing one; it carries the design
principle, not just the types. The short version: a model view is a plug, not
a page. The generic run page owns layout, the trigger form, the log pane and
all the chrome; a view supplies only what could not be generic.

The rule that decides what goes where: **a signature animation is a state
machine keyed to the run lifecycle, never a rendering of live numeric
values.** Real telemetry belongs in the diagnostics charts. And every view
carries an `honesty` note saying which parts are real and which are
decorative — that note is not garnish, it is what stops a decorative visual
being read as data. A view without one is incomplete.

`payloadOf` returns a `Partial` on purpose: a model's `payload` interface is
hand-derived from its own `emit("progress", ...)` calls, so it can go stale,
and some fields are genuinely *absent* rather than null until a run reaches a
given stage.

## What to read first

- `frontend/src/transport/hub.ts` — the header, then the tests
- `frontend/src/components/models/contract.ts` — before writing any model view
- `docs/message-envelope-spec.md` — the four message types, in full
- `app/routes/stream.py` — the SSE contract: `id:` is the message's `seq`, so
  `EventSource`'s own `Last-Event-ID` resume works with no custom handshake,
  and the server sends `retry: 2000` plus a keepalive comment every 10s
- `app/routes/runs.py` — trigger, backfill (`GET /api/runs/{id}/messages`),
  results, and cancel
- `docs/architecture.md`, "Why the client keeps its own history" — why
  backfill is user-triggered and automatic only for a finished run

## Known gaps

- **No traffic from a deployed run.** The largest one, and not fixable from
  this directory.
- **`GET /runs/:runId` is a placeholder**, not a page — see `App.tsx`. A
  finished run is currently watchable only from its model page.
- **Not every model has a view yet.** Nine are planned against
  `contract.ts`; check `src/components/models/` for which exist.
- **ADR-001 is cited and does not exist.** `vite.config.ts` and
  `useRunStream.ts` both reference it, and `.claude/agents/frontend.md`
  points at `claude/frontend-stack-adr.md` — there is no such file anywhere
  in the repo. The decisions it covers (CSR SPA, no global store) are
  described in the source headers above; the ADR itself was never written or
  was lost. Either write it or stop citing it.
- **`.claude/agents/frontend.md` still says to generate the protocol types**
  into `src/protocol.ts`. That was overtaken by the hand-written
  `src/lib/envelope.ts` plus its drift test, for the reasons above, and the
  filename it suggests now collides in spirit with
  `src/transport/protocol.ts`.
