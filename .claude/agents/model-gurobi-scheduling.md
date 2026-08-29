---
name: model-gurobi-scheduling
description: Design record for job/models/gurobi_scheduling/ (BUILT) — a staff shift-scheduling MILP using Gurobi's bundled restricted license. The original driving use case; the first model to reach an end-to-end vertical slice.
tools: Read, Write, Edit, Bash, Grep, Glob
---

## Status: built. This is the design record, not a to-do list.

`job/models/gurobi_scheduling/` exists, is tested under `tests/models/`, and is registered in
`pyproject.toml`, `deploy/requirements/`, `resources/model_gurobi_scheduling.job.yml`,
`resources/app.yml` and `uc_ddl/002_model_results.sql`. Read the source and
its tests before this file — the module docstring is maintained, this brief
is not. It is kept for the reasoning: **why** this model is in the lineup,
which is the thing the code cannot say about itself.

Two facts this brief predates and every one of these briefs got wrong:

- **This model reads real data.** All ten models read a real Unity Catalog
  table through `job/models/_data` — eight of them `samples.nyctaxi.trips`,
  this one included — falling back to a deterministic generator when there is
  no workspace, and carry `data_source` / `data_synthetic` / `data_rows` /
  `data_fallback_reason` on every result row so the two runs stay
  distinguishable afterwards. Where
  this file says "synthetic" or "small fixed problem", read "synthetic
  fallback".
- **There are ten models, and four of them have a brief.** Any count below is
  stale. The other six were built from `job/models/README.md` and `/new-model`
  with no brief at all, deliberately — see `docs/parallelization-plan.md`.

This brief was written to build `job/models/gurobi_scheduling/`. Read `CLAUDE.md`,
`docs/message-envelope-spec.md`, and `docs/architecture.md` ("Why models are
duck-typed") before writing anything.

## What this model is

A staff shift-scheduling MILP: assign staff to shifts across a planning
window subject to coverage requirements, availability, and (optionally)
preferences — a classic assignment/scheduling MIP with real branch-and-bound
behaviour, sized to fit Gurobi's bundled restricted licence.

## The one constraint that shapes everything else

**Gurobi's bundled restricted licence: 2000 variables / 2000 constraints
(200 if quadratic terms are used). No WLS, no internet call.** Keep the
model linear (no quadratic terms) and size the instance to stay comfortably
under the cap — e.g. ~20 staff × 14 days × 3 shifts ≈ 840 binary assignment
variables leaves real headroom for coverage/availability constraints. Do not
reach for WLS to get around this cap; this build deliberately doesn't use
it — see `docs/free-edition-constraints.md` for why.

Pin a `gurobipy` version deliberately and record its bundled-licence expiry
date next to the pin (each release ships a licence with a fixed expiry —
this is not a hypothetical, past releases have already expired on their
documented dates). Whoever maintains this later needs to see that date
without archaeology.

## Duck-typed surface (no base class, no inheritance)

The harness in `job/` discovers your model by convention. Expose:

- Something that builds the Gurobi model (attribute or method — build step
  optional if construction happens in `__init__`)
- The `gurobipy.Model` itself, under one of the conventional attribute names
  the harness looks for (check `job/`'s adapter/discovery code once it
  exists, or `job/models/README.md` if one gets added — don't invent your own
  name in isolation)
- A results accessor returning a list of dicts, one per output row
- Your own callback, if you use `cbLazy`/`cbCut`/`cbSetSolution` — expose it
  under a conventional name; the harness composes it with its own
  logging/cancellation observers rather than replacing it (Gurobi allows
  exactly one callback slot per model)

**Do not call `model.optimize()` yourself.** The harness owns the solve so it
can attach the composed callback (yours plus its own logging/progress/
cancellation observers). If you need custom termination logic, cuts, or
lazy constraints, expose your own callback per above rather than calling
`optimize()` directly — calling it yourself bypasses cancellation and
progress reporting entirely.

## Progress — map Gurobi's callback data onto the envelope

Emit a `progress` message roughly every few seconds (not every callback
invocation — `MIP` fires very frequently). Populate:

- `elapsed_seconds` — from `MIP_RUNTIME` or equivalent
- `primary_metric` — the MIP gap; `primary_metric_label` — `"mip_gap"`
- `payload` — the Gurobi-specific extras: `best_bound`, `incumbent`,
  `nodes_explored`, `nodes_remaining`, `solution_count`

**Handle the pre-incumbent sentinel explicitly.** Before the first feasible
solution, Gurobi's objective-best callback value is `±1e100`. Store
`incumbent` as `null` in that case — storing the raw sentinel would poison
any chart's axis downstream. Check `abs(value) < GRB.INFINITY` (or
equivalent) before treating it as real.

Log capture: `MESSAGE` fires on arbitrary text chunks, not line boundaries —
buffer and split on `\n` before emitting `log` messages, or you'll get
malformed partial lines. Set `LogToConsole=0` alongside `OutputFlag=1`, or
every line gets logged twice (once by Gurobi's own console output, once by
your capture).

## Cancellation

Poll the harness's cancellation check (whatever it's exposed as — a
callable or an event, per how `job/` wires it up) from a `POLLING` callback,
and call `model.terminate()` when it fires. This makes `optimize()` return
`INTERRUPTED` rather than raising — your code should treat that as: keep
whatever incumbent exists, write it as a result, and let the harness report
`CANCELLED` rather than `FAILED`. A user-requested stop is a clean outcome,
not an error.

## Results

Return whatever solution exists at the end of `optimize()` — including after
a cancellation, including a suboptimal-but-feasible incumbent. Never write
results only on `OPTIMAL`. One row per assignment (e.g. staff/shift/date),
as plain dicts; the harness handles getting them into the model's results
table with a row count.

## Explicit non-goals

- No WebSocket, HTTP, or Delta code — you only ever call `emit(...)`.
- No direct SQL of any kind.
- No knowledge of `run_id`, `seq`, or timestamps — the harness stamps those.
- Do not import anything from `job/`, `app/`, or `shared/`'s transport code.
  If you need the envelope's field names, they're documented in
  `docs/message-envelope-spec.md` — you're conforming to a contract, not
  calling into the platform.

## Tests to write

- The model builds and solves standalone (no harness) against a small fixed
  instance, and produces a known-feasible schedule — this is what "behaves
  identically run on its own" means in practice.
- Cancellation mid-solve on a deliberately slow instance: `terminate()` gets
  called, `optimize()` returns `INTERRUPTED`, and results still get produced
  from whatever incumbent existed.
- The `±1e100` pre-incumbent sentinel never reaches a `progress` message as
  a raw number.
- Instance size stays under 2000 variables / 2000 constraints — assert this
  in a test, not just by eyeballing the generator.
