# DBX_leaning

Databricks modelling application platform — v2 rewrite. See `CLAUDE.md` for
the full brief; this file is just "what do I do first."

## First run

```
claude
> /orient
```

That reads `CLAUDE.md` and `docs/`, and states back what it understood
before writing anything — check it agrees with you before continuing.

## Before any real building

Two platform questions gate everything else. Run them first:

```
> /spike-ws
> /spike-sse
```

Both are small, throwaway, and answer whether the transport this whole
design leans on actually works on Databricks Apps. Everything else in the
design has a documented fallback (see `docs/architecture.md`); these two
don't, so they go first.

## After the probes pass

`docs/parallelization-plan.md` has the worktree-per-track breakdown. Short
version: build `shared/` (the message envelope) once, sequentially, then
fan out — one Claude Code session per track (`app/`, `job/`, and one per
model in `models/`), each briefed from its file in `.claude/agents/`.

## What's here

```
CLAUDE.md              Project brief, auto-loaded every session
docs/                  Architecture rationale, platform constraints, envelope spec, parallel plan
.claude/agents/        One brief per parallel track
.claude/commands/      /orient, /spike-ws, /spike-sse, /new-model

shared/                The message envelope + protocol. Imported by app/ and job/,
                       never by models/. Build against this, don't fork it.
job/                   The harness: model loader, thread->loop crossing, WS client
                       with HTTP-push fallback, Delta writer, cancellation
app/                   FastAPI: SSE to browsers, WS ingress for jobs, cancel,
                       backfill, startup reconciliation, ServiceHub/DI
models/                Five model packages. See models/README.md for the
                       duck-typed contract a model has to satisfy.
uc_ddl/                Unity Catalog DDL, idempotent, apply in order
frontend/              Not started, on purpose — see frontend/README.md
tests/                 ~220 tests, none needing a Databricks connection
scripts/               check_gurobi_licence.py — the bundled-licence expiry
```

## Running it locally

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[app,job,models,dev]"

pytest                                  # everything, offline

# a full run with no app listening — the normal unobserved case
DBX_MODEL=models.scenario DBX_WRITER=jsonl DBX_ALLOW_LOCAL_WRITER=1 \
  python -m job.main

uvicorn app.main:app --reload           # the observer, on :8000
```

Model extras are separable: `pip install -e ".[job,gurobi]"` gets you the
scheduling model without pulling in scikit-learn or emcee.

## State of play

`shared/`, `job/`, `app/` and all five models are built and tested. What is
**not** done is the part no amount of local testing can settle: neither
ingress probe has run (`docs/spike-results.md`), so which live channel this
actually uses in production is still unknown. Both paths are implemented and
the code degrades cleanly either way — but "designed for" is not "measured".
