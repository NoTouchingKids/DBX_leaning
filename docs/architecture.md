# Architecture — condensed rationale

This is *why*, compressed from a longer design conversation. It exists so a
session (or an agent working one track) can understand a decision without
re-deriving it or, worse, re-opening it. If you think one of these is wrong,
that's a fair thing to raise — but raise it, don't quietly build around it.

## Where this comes from

Two earlier builds precede this one, in the same project:

1. **A Flask + Streamlit POC**, polling-based, that deliberately avoided
   WebSockets/SSE on the strength of *reported* (not officially documented)
   Databricks Apps ingress behaviour — idle connections dropped ~30s, long
   streams cut ~120s.
2. **A FastAPI + WebSocket rewrite**, feature-complete, well-tested (123
   automated checks), and **never deployed**. Its own handover doc listed
   "verify the WebSocket survives the ingress" as unfinished item #1 — and
   that verification never happened.

This rewrite inherits the same open question a third time, plus a design
goal the first two didn't have: **this one has to run on Free Edition and be
cost-effective**, not just architecturally clean.

## The transport decision, and why it looks the way it does

**WS job→app, SSE app→client, Delta always.** Not WS end-to-end, because the
two hops have different shapes: job→app is bidirectional (cancel needs to
travel back), long-lived, and both ends are under our control. App→client is
one-way in the common case, and `EventSource` gives reconnect and a resume
cursor (`Last-Event-ID`) for free — no reason to hand-roll that over WS.

**There is one live channel job→app, and there used to be two.** The original
design put HTTP push behind the WebSocket as a fallback, for a socket nobody
had yet proved would survive the Databricks Apps ingress. The probe settled
that on 2026-08-23 (`docs/spike-results.md`), and once the socket is known to
work the second path costs more than it buys. It is one-way, so it can carry
neither a cancel nor a backfill request. It is a second code path that has to
produce results identical to the first or a run looks different depending on
which one happened to carry it. And the `LiveChannel` Protocol and preference
ordering that made the two interchangeable are ceremony the moment there is
only one implementation to order. So `job/channels.py` and `job/relay.py` are
gone, replaced by `job/bus.py`. The app's `/api/runs/{run_id}/push` ingest
endpoint was deliberately left standing; the job no longer sends to it.

**The tiered drop policy went with it, and could only go once backfill
existed.** The old relay evicted logs before progress before status, on the
argument that a dropped message was gone for good so the cheapest thing to
lose should go first. That was correct while its premise held. The premise
does not hold any more: everything offered to the bus is already in the
durable buffer *and* in the job's replay ring, so dropping the oldest —
whatever type it is — costs a client a gap it can ask to have filled. Simple
and recoverable beats clever and lossy, but the order of those two changes
mattered: simplifying the policy before the ring existed would have been a
straight regression.

**Delta is not tier 3 of a fallback ladder — it's the floor**, running always
in parallel with whichever live tier is up. If it were only a fallback, a run
watched live over WS would never get persisted, and backfill/history would
silently not exist for the runs that happened to have a good connection.

**The job is autonomous; the app is an optional observer**, not the other
way around. This matters because Free Edition apps run ~8h/day and jobs
don't share that schedule — a job triggered at 3am must behave identically
whether or not anything is listening. "The app watches when it can" is the
correct mental model; "the app orchestrates and the job reports to it" is
not, and would break the moment the app is down when a job starts.

**Cancel goes through the app, never a warehouse poll.** A client polling
`run_status` on a timer to check for a cancel flag would keep the SQL
warehouse awake for the run's entire duration — which is close to exactly
the cost mistake the first (polling) build made, just relocated to a
different table.

## Why the SQL warehouse is read-only in this design

Serverless SQL warehouse cost is driven by **uptime**, not statement count or
data volume (see `docs/free-edition-constraints.md`). The first build's
real-world cost blowup (tens of dollars in testing, vs. cents for equivalent
WebSocket traffic) came from *keeping the warehouse awake* via 1.5-second
polling — not from the size or number of individual writes. Moving the write
path to Delta doesn't save on "fewer statements"; it saves because **the
warehouse never wakes up for a write at all**. Reads (backfill on connect,
occasional reconciliation) are fine because they're infrequent enough not to
matter; the discipline to hold onto is "does this touch the warehouse on a
short, regular interval" — if yes, it will cost real money regardless of how
small the query is.

## Why the job can be asked, and not only heard

The job keeps a `RunRecord` (`job/record.py`): its latest status, a bounded
progress history, and a bounded ring of recent messages. The ring is what
turns the job from something that only emits into something that can be
*asked*, and it exists for the cost argument directly above. A browser tab
backgrounded for thirty seconds reconnects with a gap of a few dozen messages.
Serving that from Unity Catalog is a `SELECT`, which means the warehouse is
awake, which means an idle user flicking between tabs is a way to spend money.
Served from the job's own memory it is one WebSocket frame.

Why a ring and not "whatever is still in the durable buffer": the buffer is
emptied on every flush, so it can only ever answer for rows Delta does not
have yet, and a client whose gap straddles a flush would get half an answer
with nothing to tell it so. The ring is independent of flushing and covers
both sides of it. It withholds `client_visible=false` logs, because the
warehouse backfill withholds them too — two sources answering the same
question differently is the precise failure the one-envelope design exists to
prevent, and raw solver chatter that appeared only when the job happened to
still be holding it would be exactly that.

**The boundary between "ask the job" and "ask SQL" is a fact on the wire, not
a constant either side keeps.** The job states two numbers on `hello` and on
every backfill reply: `replay_from_seq`, the oldest seq it can still replay,
and `flushed_through_seq`, how far Delta has caught up. Above the ring's floor
the job can answer completely; below it, only the warehouse can. A tuned
threshold — "fetch under 500 messages from the job" — would be a number to
keep in step across two codebases, and it would be wrong the first time a
model changed how chattily it logs. Both bounds are things the job knows for
free, so it says them and the app decides.

`flushed_through_seq` is deliberately not "the highest seq written". Tables
flush independently, so if `run_logs` has gone out to seq 100 while
`run_progress` still holds seq 50, the warehouse cannot serve 50 and saying it
can would send a client to fetch a row that is not there. The high-water mark
a reader can trust stops one below the lowest thing still pending — see
`DurableBuffer.min_pending_seq`.

**Both sides of this are built, and the app is the one that decides.**
`app/server/services.py::JobConnections` records the bounds off `hello` and off
every `backfill_result`, so a long-lived connection's picture does not go stale
while the ring moves; `can_serve` answers "socket here, bounds stated, gap at
or above the floor", and `GET /api/runs/{run_id}/messages` asks the job
whenever it says yes. A reply carrying `complete: true` is returned without
touching SQL.
Everything else — no socket, no bounds stated yet, a gap below the floor, a job
that does not answer inside `BACKFILL_TIMEOUT_S` — falls through to
`repository.messages_since` exactly as before. The response names which
answered in `source`, because "did that read wake the warehouse" should be
readable off a response rather than out of the app's log.

Three details there are decisions rather than accidents. A job that has stated
no bounds counts as cannot-serve, so a socket whose `hello` has not landed
costs one fallback read rather than a timeout on every request. A second
concurrent backfill for the same run is refused to the warehouse rather than
parked on the first request's future: two waiters woken by whichever reply
lands first would each be handed a page computed for the other's cursor, and
silently wrong messages are worse than a rare warehouse read. And a
`complete: false` page is discarded rather than stitched onto the SQL one —
merging two sources into a single page is a dedupe problem, and the warehouse
can serve the whole gap by itself.

## Why teardown drains before it closes

This was a real bug and the shape of it is worth keeping. The old teardown
called `relay.stop()`, which closed the channels, and *then* let the pump run
out — so the pump drained into a shut socket and counted everything it held as
dropped. On a long run it was invisible, because the queue was empty by then
anyway. On a run that finishes faster than the socket can flush it was total:
0% of the live stream delivered, terminal status included, with only 5ms of
channel latency. The durable path was unaffected the whole time, which is
exactly why nothing caught it — the run was correct and complete, and the
browser watching it simply saw nothing.

`WebSocketBus.drain(timeout_s)` now runs first and `close()` second, bounded
by `DBX_WS_DRAIN_S` (default 5s) so that a wedged socket cannot hold a
finished run open.

That bound then produced a smaller version of the same problem, and the fix
lives in the same place. FIFO is right for a live view — a client wants the
order things happened in — but the terminal status is the *last* message
emitted and therefore the last thing a bounded drain reaches, so a backlog the
deadline cannot clear loses precisely the message that matters most: a
300-message run over a 20ms socket drained 81% of its queue and the status was
not in it. So at teardown, and only at teardown, status and result jump ahead
of logs and progress. The client sees a gap in `seq`, which is a thing it
knows what to do with. An outcome plus a recoverable hole beats a tidy prefix
and no outcome.

## Who writes `run_status`, and why it is two writers

`run_status` is one row per run in Lakebase, and it used to be written only by
the app, from status messages arriving over the socket. That made a fact about
the run depend on the observer being up and the socket being healthy, which is
backwards for a platform whose first invariant is that the job is autonomous:
a run triggered at 3am with the app down sat at whatever the app last saw
until startup reconciliation noticed. The job knows its own status, so the job
reports it (`job/lakebase.py`).

Over the Database REST API rather than a Postgres driver, because a driver in
this process would be paid for by all ten model environments — one job per
model, each with its own dependency list — and `httpx` is already present for
the OAuth exchange in `job/auth.py`.

The split is by concern, not by row. The **app** owns the slot claim: the
count-and-claim transaction that makes the 5-concurrent-task ceiling real
rather than advisory, and the row's creation at trigger time. The **job** owns
the status transitions — the upsert on `run_status`, and the append that
records the same transition in `run_status_history` (see the next section).
The job's write is an upsert rather than an
update, because a job triggered outside the app (a schedule, a manual
`run-now`) has no row yet, and refusing to record the status of exactly the
runs nobody is watching is the wrong way to fail. It carries a
`WHERE ... updated_ts <= EXCLUDED.updated_ts` guard, because Databricks can
deliver a retry out of order and without it a late `RUNNING` would overwrite a
`SUCCEEDED` that had already landed. The guard is about the row and only the
row: an append-only history has nothing to move backwards, so a retry
delivered late lands there as one more row rather than as a lost transition.

One asymmetry to know about: the app still calls `set_status` for every status
message it sees on the socket, and *its* upsert has no such guard
(`app/server/store.py`). Both writers are writing the same job's transitions,
so in the ordinary case they agree; the ordering hazard that is left on this
row is the app applying something it observed late.

None of it is load-bearing: unconfigured, unreachable, refused — all of them
log and carry on, and every transition is durably recorded in `run_events`
regardless.

**The one thing here that is not verified is the request envelope.**
`LakebaseStatus._body()` is the single place it is constructed — deliberately,
so pointing it at a different shape is a change in one method — and it has
never been sent to a live workspace. One real request and response would
settle it; until then, treat the shape as unconfirmed.

## What lives in Postgres, what lives in Delta

The line is OLTP-shaped state on one side and append-only telemetry on the
other. Where it falls is worth writing down, because the two obvious tidyings
both cross it: "state is state, so move all of it to Postgres" drags progress
along with it, and "there is already a status table in Postgres, so delete the
Delta one" takes out a quarter of the message stream.

**Postgres holds two tables.** `run_status` is current state — one row per
run, `run_id` as PRIMARY KEY, upserted on every transition.
`run_status_history` is append-only beside it, one row per transition, and it
is where "what did this look like an hour ago" and "how did this run actually
end" are answered. Those questions were previously answerable only by reading
`run_events` back out of Delta, which means waking the SQL warehouse to ask
them.

**One writer of that table is designed and absent, and it is worth being
precise about which.** Only the job appends. `recorded_by` defaults to `'job'`
and the unique index is partial — `UNIQUE (run_id, seq) WHERE seq IS NOT NULL`
— specifically so a writer with no envelope message behind it, and therefore
no `seq`, can still append; that writer is the app at slot-claim time, and it
does not exist. `claim_slot` creates the `run_status` row and writes no history
row, so a run's history begins at the job's first report and **no run's
history contains its `QUEUED`**. "How long did this sit `QUEUED`" is the
obvious question to bring to a transition log and the one this one cannot
answer yet; do not read the schema as evidence that it can. Reconciliation is
unaffected — it reads the newest transition, which is always the job's.

**`run_status` itself does not become append-only, and that is deliberate.** It
is the tempting simplification — one table, insert-only, no upsert guard to
get right — and it would give back both of the things Postgres was chosen over
Delta for in the first place. The primary key on `run_id` is what refuses a
duplicate run outright, rather than quietly producing a second registry row and
leaving every point lookup afterwards to decide which of the two is the run.
And the 5-concurrent-task ceiling is a count-and-claim inside one transaction:
count the non-terminal rows, insert, commit. Append-only turns that count into
latest-row-per-run — a window function over the whole history, on the hot path
of every trigger, in place of an indexed count over a partial index that
already excludes the terminal rows. The appends go to the history table; the
row the ceiling is enforced on stays one row.

**The planned end-of-run status write to Unity Catalog's `run_status` is
cancelled**, and was never built. Its purpose was to leave a run's final state
somewhere durable that did not depend on the live path. Lakebase now holds both
the current row and the transitions that produced it, so a Delta row repeating
the same fact is a third copy to keep in step. UC's `run_status` stays in
`uc_ddl/` as what it already was: the fallback home for the row on a deployment
with no Lakebase provisioned.

**`run_events` survives, and its own DDL comment is wrong about why.** The
comment in `uc_ddl/001_core_tables.sql` says the table exists so that a job
running while the app is down still records its transitions somewhere startup
reconciliation can read them back from. That was the whole stated
justification, and it largely evaporated once the job started writing Lakebase
directly: the run the app missed now updates the registry itself, as it
happens. Read on its own, that comment now argues for deleting the table. The
comment is what is out of date, not the table — this section is the correction
until the DDL says it itself.

What `run_events` actually is: **the `status` quarter of the message stream.**
`app/server/repository.py::messages_since` — the backfill query — is a
`UNION ALL` over `run_logs`, `run_progress`, `run_events` and
`run_results_meta`, ordered by the single monotonic `seq`. Take `run_events`
out and a client backfilling a finished run gets a stream with a permanent hole
wherever a status transition was, and "did this run succeed" stops being
answerable from the message stream at all. The replacement would be the client
fetching status separately, from a different store, in a different shape, and
merging it in by timestamp — because `seq` is assigned by the job onto the
envelope, and a Postgres row that was never an envelope does not carry one.
That is two shapes for one thing, which is the exact failure the one-envelope
design exists to prevent.

The second reason to keep it is that `run_events` is the only status record
that does not depend on a network call succeeding. Lakebase-over-REST is a live
path, and unconfigured, unreachable and refused are all things it does — all
of them logged and carried on from. `run_events` rides the Spark durable path,
which is the floor. The price of that insurance is two or three rows per run,
on a flush that is happening anyway.

What did change is its **role**: from reconciliation's primary source to its
backstop, with Lakebase primary. That is a real win rather than a relabelling.
`app/server/reconcile.py` now asks three sources in order — `run_status_history`
in Postgres, then `run_events` in Delta, then the Jobs API — cheapest and
closest to the job first. Answering "what happened to the runs I missed" used
to mean waking the SQL warehouse at app startup for a question Postgres can
answer for nothing. The warehouse is now the second call, for a deploy with no
Postgres or a report that never reached it, and `repo` stays optional so a
Lakebase-only deploy reconciles from the transition log and the Jobs API
instead of not reconciling at all.

**Progress stays in Delta.** It reads like state — "where has this run got
to" — and it is telemetry on every measure that made status OLTP-shaped.
Volume: 10 to 500 progress rows in a run, against two or three status
transitions. Shape: a status row is two short strings, while a progress row
carries a free-form `payload_json` that `mcmc` fills with `chain_positions` and
`panel_fit` with `failure_counts`. Access: every question asked of progress is
an analytical scan — plot the objective over the run, compare this run's
convergence with the last one's — never a point lookup, and never a count
against a ceiling.

The live path makes that sharper rather than softer. Progress is barely read
from storage at all: while the run is live it arrives over the WebSocket, a
small gap is served from the job's replay ring, and storage is touched only for
a cold read of a finished run — which is precisely the analytical case, one
ordered scan of one run's rows, on a warehouse the rest of the backfill is
waking anyway. Moving it to Postgres would pay OLTP prices for a workload that
never does a point lookup, in the one store whose job is a transactional count.

## Why models are duck-typed, not a class hierarchy

An earlier draft of this platform's architecture proposed a `ModelAdapter`
base class with subclasses per model family (`GurobiAdapter`,
`ForecastAdapter`, etc.), declared before a second model type existed. That's
backwards: build the general case *after* there's a second concrete case to
generalise from, not before. The first working build of this pattern (the
`dispatch-app` prototype) got real mileage from pure duck typing — the
harness looks for a small set of conventional attribute/method names on
whatever object it's given, with no inheritance required — and that's the
model this rewrite keeps. A model needs no import from the platform and
behaves identically run standalone; the harness fails with a readable message
listing what it tried, if it can't find what it needs.

Concretely: a model exposes (by convention, not inheritance) something that
builds itself, something that runs, a way to check/request cancellation, and
a way to get results out. It's handed an `emit()` callback for envelope
messages. That's the entire coupling surface.

## Why the message envelope is one shape

See `docs/message-envelope-spec.md` for the contract itself. The short
version: `log`/`progress`/`status`/`result` are four kinds of "something
that happened during a run," and giving them one shape with a `type`
discriminator means the transport never needs to know what it's carrying —
the identical object goes over the WebSocket, comes back in a backfill reply,
or gets read back out of Delta. Two shapes (a wire shape and a storage shape)
is how the first build's real bugs happened: a mismatch that only ever
surfaced after a reconnect, because that's the one code path that exercises
both shapes at once.

## Why logs and results have different durability rules

Logs exist for progress display and debugging. They're allowed to drop on
the live path under pressure — the durable Delta write is what actually
matters, and it doesn't drop. Results are different: a model's output is the
actual point of running it, so results get written the moment the code
produces them, independent of what the final status turns out to be (a
cancelled run keeps its incumbent). The one thing that must never happen is
a run claiming `SUCCEEDED` while its result write silently failed — that's
worse than an honest `FAILED`, because nobody goes looking for a problem that
claims not to exist. This is why `result.row_count` exists in the envelope:
it's the difference between "succeeded, wrote rows" and "succeeded, wrote
nothing," visible without reading logs.

## Why the client keeps its own history

An in-app server-side ring buffer (per active run) was considered and
dropped in favour of client-side caching with **user-triggered backfill on
reconnect, automatic only on first view of a run**. Two things make this the
better trade: a finished run is immutable, so once a client has fetched it,
it can cache it forever and never re-fetch; and most reconnects (a routine
~120s SSE cut, immediately re-established) produce a gap of milliseconds —
usually nothing — so demanding the user click "load missing" for a genuine,
large gap (they were actually away) is honest rather than annoying. The one
trap: the client's retry-then-give-up counter must count *consecutive
connection failures*, resetting to zero on every successful reconnect — a
naive "stop after 3 tries" would kill a perfectly healthy stream a few
minutes in, if the ingress does in fact cut connections periodically.

This is built, and it landed with one correction to the reasoning above. A
gap is not only "records that have not arrived yet": the live path never
sends `client_visible=false` logs and the backfill endpoint filters them out
too, so some holes are **permanent by design**, and a client that loops
"backfill until contiguous" spins forever. So gaps are reported and never
acted on automatically. `app/client/src/transport/hub.ts` owns the
consecutive-failure counter (capped at 10, reset on every successful open)
and the gap detection; `app/client/src/hooks/useApi.ts` has the two fetch
shapes the argument above asks for — `useTerminalHistory`, the one automatic
fetch a finished run gets, cached for the session because a finished run is
immutable, and `useFetchGap`, the user-triggered one. Neither is on a timer,
and React Query is configured `refetchInterval: false` for the same
warehouse-uptime reason the write path avoids the warehouse.

The job's replay ring is not a reversal of the first sentence above, and it is
worth being explicit because it reads like one. What was rejected was a ring
*in the app*, per active run, standing in for the client caching what it has
already seen — that would have put the app on the critical path for history it
does not own. `job/record.py` is somewhere else answering a different
question: the run's own author holding its last few thousand messages so that
a small gap need not become a warehouse query. The client still caches, still
asks before it fetches, and still never polls.

## Why Gurobi uses the bundled licence, not WLS

This project only needs Gurobi to prove the platform's transport and message
envelope work end to end for a real MILP — it is a test model, not a
production optimisation deployment. WLS needs outbound internet to
`token.gurobi.com`, which Free Edition restricts by default. The bundled
restricted licence needs no network call and is free, at the cost of a
2000-variable/2000-constraint cap — comfortably enough to build a real
scheduling model with genuine branch-and-bound behaviour to stream. See
`docs/free-edition-constraints.md` for the licence-expiry gotcha that comes
with this choice.

The cap and the expiry are both real costs, and `job/models/ortools_jobshop` is
what pays neither: CP-SAT is Apache-2.0, has no licence file, no expiry date,
nothing to reach over the network, and no size limit. That is not an argument
for dropping Gurobi — the two solve different problems with different search
paradigms, and the contrast between a MIP callback and a solution callback is
itself worth having on a platform whose subject is telemetry. It is the
answer to "what happens when a model outgrows the restricted licence", which
until it existed was "nothing good".

## Why Spark writes Delta, and why delta-rs is gone

This one changed shape twice after it was designed, and both changes are worth
recording because the original reasoning was sound and the conclusion was
still wrong.

The intent was delta-rs (`deltalake`) as the writer, with Spark as the
fallback — a small pure-Python dependency in preference to standing up a
Spark session in every job. What was missed is that `write_deltalake()` takes
a *path or storage URI*, not a Unity Catalog table name, and handed a
three-part name like `"main.dbx_leaning.run_logs"` it does not raise. It
creates a local directory with that literal name and writes there. Verified
2026-08-23. A job doing that would report `SUCCEEDED` with an accurate
`row_count` while its telemetry sat in a container filesystem about to
disappear. That is the exact failure this platform's durability rules exist
to prevent, delivered by the component whose job was to guarantee against
it — and it would have been silent.

So the positions swap. **Spark is the write path, not a fallback**: on
Databricks serverless a session already exists, so its cost is paid once per
run rather than per flush, and at ~1MB/30s flush granularity Delta commit
overhead is per-flush anyway.

The second change is that `DeltaRsWriter` has been **deleted**, rather than
kept as a class that raises. Keeping it was meant to leave the interface it
ought to satisfy visible. What it actually left visible was a
selectable-looking name for the failure above — one string literal away from
being chosen, in the one selector where being wrong means a run reports
SUCCEEDED over telemetry that no longer exists. `WriterKind` now has three
members and all three are real. Making delta-rs work still needs the table
resolved to a storage location and credentials obtained via Unity Catalog
credential vending (see `job/delta.py`); that is a new piece of work, and the
deleted class was never a head start on it. The other implementation,
`JsonlWriter`, exists so the whole harness can be exercised with no Databricks
connection at all — which is what makes "the app is down and the job runs
anyway" a testable property rather than an aspiration. `auto` will only fall back to it
when `DBX_ALLOW_LOCAL_WRITER=1` is set, and otherwise raises: silently
writing a real run's telemetry to a file the container throws away is worse
than failing.

## What was unverified, and what still is

Two things this design couldn't get right by reasoning alone needed to be run
against the platform, and they gated everything else:

1. **Does the Databricks Apps ingress pass a WebSocket `Upgrade` and hold it
   idle?** (`/spike-ws`)
2. **Does the ingress buffer or cut an SSE stream, and at what duration?**
   (`/spike-sse`)

**Both are answered: yes and yes**, confirmed against a real workspace on
2026-08-23 — see `docs/spike-results.md`. That is the question that stayed
open across all three builds of this platform, and it is now closed. The
transport above is the one being built, not a hopeful guess.

What is still *not* measured is the numbers, and they matter to specific code:
whether the ingress cuts a long-lived stream and at what elapsed time, whether
an idle connection goes sooner than an active one, and whether SSE events
arrive promptly or in held-and-released batches. `DBX_WS_PING_S` (20s) and
`DBX_SSE_KEEPALIVE_S` (10s) are conservative guesses from community reports
until those land. `docs/spike-results.md` has the table to fill in.

One more thing is unverified, and it is narrower than it was. The Database
REST API's request *envelope* in `job/lakebase.py` has never been sent to a
live workspace — see "Who writes `run_status`" above; the `run_status` upsert
and the `run_status_history` append both travel over it, so one real request
settles both. What is no longer unverified is the statement inside it:
`tests/app/test_run_store.py` imports `REPORT_SQL` rather than retyping it and
executes it against a real PostgreSQL 16, over the `run_status_history` DDL
this repo ships, against the same table the app's own `set_status` writes. So
the SQL, the schema and the two writers' agreement about which direction is
backwards are all tested; the HTTP body around them is what is not.

Everything else in this design has a documented fallback if it doesn't pan
out (VARIANT → a JSON string column, and the whole live path → Delta alone).
Two of those fallbacks have since been spent rather than kept. The Delta
writer inverted itself: delta-rs was the intended implementation with Spark as
the fallback, and delta-rs turned out to be unable to address a Unity Catalog
table by name at all, so Spark is the write path and delta-rs is deleted — see
"Why Spark writes Delta, and why delta-rs is gone" above. HTTP push was the
live path's fallback and is gone from the job for the opposite reason: the
thing it hedged against does not happen, and the probes are what established
that.
