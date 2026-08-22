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
```

Nothing under `app/`, `job/`, `models/`, `frontend/` exists yet — this
scaffold is the brief, not the build.
