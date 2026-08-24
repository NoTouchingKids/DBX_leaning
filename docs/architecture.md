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
the identical object goes over WebSocket, over HTTP push, or gets read back
out of Delta. Two shapes (a wire shape and a storage shape) is how the first
build's real bugs happened: a mismatch that only ever surfaced after a
reconnect, because that's the one code path that exercises both shapes at
once.

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
acted on automatically. `frontend/src/transport/hub.ts` owns the
consecutive-failure counter (capped at 10, reset on every successful open)
and the gap detection; `frontend/src/hooks/useApi.ts` has the two fetch
shapes the argument above asks for — `useTerminalHistory`, the one automatic
fetch a finished run gets, cached for the session because a finished run is
immutable, and `useFetchGap`, the user-triggered one. Neither is on a timer,
and React Query is configured `refetchInterval: false` for the same
warehouse-uptime reason the write path avoids the warehouse.

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

## Why Spark writes Delta, and delta-rs does not

This one changed shape after it was designed, and the change is worth
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
overhead is per-flush anyway. `DeltaRsWriter` is kept as a named class so the
interface it is meant to satisfy stays visible, and it raises
`NotImplementedError` with the reason rather than doing the quiet thing;
`select_writer("auto")` never picks it. Making it real needs the table
resolved to a storage location and credentials obtained via Unity Catalog
credential vending — see `job/delta.py`. A third implementation, `JsonlWriter`,
exists so the whole harness can be exercised with no Databricks connection at
all — which is what makes "the app is down and the job runs anyway" a
testable property rather than an aspiration. `auto` will only fall back to it
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

Everything else in this design has a documented fallback if it doesn't pan
out (VARIANT → a JSON string column, and the whole live path → Delta alone).
The one fallback that inverted itself is the Delta writer: delta-rs was the
intended implementation with Spark as the fallback, and it turned out
delta-rs cannot address a Unity Catalog table by name at all — see the next
section.
