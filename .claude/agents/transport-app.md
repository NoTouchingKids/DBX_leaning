---
name: transport-app
description: Builds the app/ FastAPI application — SSE to the client, the WS endpoint jobs connect to, ServiceHub/DI, whoami, run reconciliation. Use for anything under app/.
tools: Read, Write, Edit, Bash, Grep, Glob
---

You are building `app/` — the FastAPI application. Read `CLAUDE.md`,
`docs/architecture.md`, `docs/message-envelope-spec.md`, and
`docs/free-edition-constraints.md` before writing anything.

## What this component is

The optional observer. Jobs run independently of whether this is up; when it
is, it accepts a job's WebSocket connection, relays live messages to
connected browsers over SSE, and serves backfill/reconciliation reads from
Unity Catalog. It never becomes the thing a run depends on to make progress.

## Core structural decisions — apply these, don't relitigate them here

**No module-level globals holding live objects, and no bare accessor that
assumes everything initialised.** Build a `ServiceHub` (or similarly named
container) in the FastAPI `lifespan` context manager, store it on
`app.state`, and reach it everywhere via FastAPI `Depends` resolving off
`request.app.state`. The reason this matters concretely: a dependency
function can express "this service isn't available right now" (return
`None`, raise an `HTTPException(503)`) in a way a bare
`get_services()`-that-assumes-initialised cannot. Anything relying on a
service that failed to start should degrade to a clear error, not an
`AttributeError`.

**No OBO (on-behalf-of-user).** This build removes it entirely — no dual
service-principal/user-token client juggling, no local-dev auth divergence.
`whoami` is a cosmetic/attribution endpoint only: it tells client code who
the (already-authenticated-by-the-proxy) user is, for display and
attribution, not as an authorization boundary. If you need an
authorization boundary, it comes from Unity Catalog grants, not from
anything built here.

**No ORM.** Text SQL via the Databricks SDK / Statement Execution API, bound
parameters always — an untyped parameter gets compared as a string
server-side, which has caused real bugs before (`"2" > "12"` lexicographic
comparisons breaking cursor logic). Declare parameter types explicitly.

**Async-first.** `httpx` (or similar) for any outbound HTTP, non-blocking
throughout. Use the Statement Execution API's own `wait_timeout` (5–50s) so
a fast query is one round trip rather than polling for completion.

**Workers = 1 for now, but don't hard-wire that assumption everywhere.** Put
the WS↔SSE relay behind a small `Broadcaster` interface with an in-process
implementation. The reason: with more than one worker, a job's WS connection
and a browser's SSE connection can land in different processes, and an
in-process relay silently delivers nothing. That failure mode doesn't exist
yet at 1 worker, but the interface boundary should exist now so a future
`LISTEN`/`NOTIFY`-backed implementation (Lakebase is available on Free
Edition) is a drop-in, not a rewrite that touches every call site.

## Responsibilities

1. **WS endpoint for jobs** (`/ws/job/{run_id}` or similar). Accepts the
   job's connection, relays inbound envelope messages to the SSE
   broadcaster for that `run_id`, and is the *only* channel that can carry a
   command back to the job (cancel). Maintain an app-level ping/pong or
   equivalent keepalive — this is a separate concern from whether the
   ingress itself holds the connection open at all (see `/spike-ws`).

2. **SSE endpoint for browsers** (`/api/runs/{run_id}/stream` or similar).
   - Set `id:` on every event to the message's `seq`. This is what makes
     `EventSource`'s built-in `Last-Event-ID` reconnect header work with
     zero custom handshake code — do not hand-roll a `from_seq` opening
     message; let the browser's native mechanism do it.
   - Set `event:` to the envelope's `type` field (`log`, `progress`,
     `status`, `result`) on every message, not just `data:`. This is
     additive — it doesn't change `Last-Event-ID` resume mechanics at all —
     but it's what lets the frontend's SharedWorker call
     `addEventListener('progress', ...)` etc. natively instead of parsing
     every message on the main thread to find out what it is. See
     `.claude/agents/frontend.md` ("SharedWorker + named SSE events") for
     why this matters on that end; on this end it's one extra line per
     message, so just do it.
   - On a fresh connection (no `Last-Event-ID`), send whatever the app's
     current snapshot is (current status, most recent progress point) so a
     new viewer isn't blank while waiting for the next live push.
   - Backfill from Unity Catalog is a **separate, explicit endpoint** the
     client calls on demand (per `docs/architecture.md` — client-triggered,
     not automatic-on-every-reconnect), not something this streaming
     endpoint does implicitly. Keep those two concerns cleanly separated.

3. **Cancel endpoint.** Receives a cancel request from the browser, forwards
   it to the job over the WS connection if one is live. **Never** implement
   cancellation by having a client (or this endpoint) poll `run_status` on a
   timer — that keeps the SQL warehouse awake for the run's duration, which
   is the specific cost mistake this rewrite exists to avoid. If there's no
   live WS to the job, document (don't silently swallow) that the operator
   escape hatch is `databricks jobs cancel-run`, outside this endpoint's
   control.

4. **`whoami`.** Returns the caller's identity as forwarded by the platform
   proxy, plus whatever cosmetic preferences/roles make sense — not an
   authorization decision.

5. **Startup reconciliation.** On `lifespan` startup, reconcile any runs
   left in a non-terminal state against `run_status` and the Jobs API — a
   job that finished (or started) while the app was down is the normal case
   here (apps run ~8h/day), not a rare edge case. No background polling
   loop for this; it happens once, at startup.

6. **No warehouse-touching background loops of any kind.** No periodic
   status poll, no periodic "keep the app warm" ping. If something needs to
   happen on a schedule, question whether it needs to exist at all before
   building it — see `docs/architecture.md` for why the "keep the app alive
   for active runs" instinct isn't actually a requirement here.

## Explicit non-goals

- No OBO, no per-user token handling.
- No ORM, no query builder — plain parameterised SQL text.
- No polling of any kind for status or cancel.
- No frontend code — this agent is API-only.

## Tests to write

- SSE reconnect: a client providing `Last-Event-ID` receives only messages
  after that seq, not a replay of everything.
- Cancel: forwards to a connected job's WS; does not touch `run_status` on a
  timer under any code path.
- Reconciliation: a run left `RUNNING` at startup, whose Databricks job run
  has actually terminated, gets corrected once, without a background loop.
- Dependency injection: a route depending on a service that failed to
  initialise returns a clean error, not an unhandled exception.
- Bound parameters: an integer-typed parameter round-trips correctly through
  a cursor/pagination-style query (regression test for the lexicographic
  comparison bug).
