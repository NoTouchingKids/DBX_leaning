# The dev stack in a container

```bash
docker compose -f docker/compose.yml up --build
```

Then:

- **trigger** — `http://127.0.0.1:8000/docs` → `POST /api/runs` →
  `{"model": "heartbeat", "config": {"duration_seconds": 600}}`
- **watch** — open the `stream` path from the 202 response in a new tab:
  `http://127.0.0.1:8000/api/runs/<run_id>/stream`
- **cancel** — `POST /api/runs/{run_id}/cancel`, same page
- **the SPA** — `cd app/client && npm run dev`; its proxy already targets
  `127.0.0.1:8000`, so the container is indistinguishable from the bare script

`docker compose down` stops everything and discards the registry.

## Use this, or don't

`uv run python scripts/dev_stack.py` needs no Docker, starts in seconds and is
the faster loop. Reach for the container when one of these applies:

- **You are on aarch64 Linux.** `pgserver` — the embedded Postgres the bare
  script uses — publishes no wheels for it (see the marker in
  `pyproject.toml`), so the embedded path cannot run at all.
- **You want a real Postgres.** `postgres:16` is the same major version a live
  Lakebase instance reported (`PostgresRunStore.server_version` exists because
  this repo once asserted 18 and was wrong).
- **You do not want uv or Python 3.11 on the host.**

Everything else is the same code either way. The container runs
`scripts/dev_stack.py` with `--postgres-dsn` pointing at the `postgres`
service and `--host 0.0.0.0`; there is no container-only code path.

## What is real and what is not

Same split as the bare script — `scripts/dev_stack.py`'s docstring has it in
full — with one addition that belongs to the container specifically:

**The per-model dependency split is NOT reproduced.** One container carries
every model's libraries, because a container is one environment and the split
is one environment per *job*. `deploy/requirements/<model>.txt` is what holds
it and `tests/deploy/` is what checks it; a green run here says nothing about
whether the MCMC job accidentally carries gurobipy.

Worth stating for the same reason: this Dockerfile is **not** a picture of the
deployment. Databricks Apps builds the app from `app/requirements.txt`, and a
serverless task installs one model's requirements file. Neither reads anything
under `docker/`.

## Running one job with no app at all

The job is autonomous — that is the platform's first invariant — so it does not
need the stack. Omit `DBX_APP_URL` and it runs unobserved, durable path only:

```bash
docker compose -f docker/compose.yml run --rm --no-deps stack \
  uv run python job/run_model.py \
    DBX_RUN_ID=local-1 \
    DBX_MODEL=job.models.heartbeat \
    'DBX_MODEL_CONFIG={"duration_seconds": 60, "log_interval_seconds": 5}' \
    DBX_WRITER=jsonl
```

`--no-deps` skips Postgres: with no `DBX_LAKEBASE_DSN` there is no status
reporting to do, and the run says so rather than failing. This is the fastest
way to see whether a MODEL works, with none of the transport in the way.

## Editing while it runs

The repo is mounted at `/work`, so host edits are live. `app/` changes need
the stack restarted (`docker compose restart stack`) unless you add `--reload`
to the command; `job/models/` changes need nothing, since each run spawns a
fresh process.

Three paths are deliberately masked with anonymous volumes and stay the
container's own: `.venv` (built for linux and the container's Python — a host
one is the wrong interpreter and the wrong wheels), `app/client/node_modules`,
and `.git`.

State the stack owns — JSONL telemetry and job logs — lands in
`.docker-dev-state/` at the repo root, readable from the host while a run is
still going. It is gitignored.

## Verified, and not

`--postgres-dsn` and `--host` were exercised against a real external Postgres:
the stack came up, a `heartbeat` run streamed over SSE and reached SUCCEEDED,
its row landed in that Postgres, and teardown left the server running. **The
container itself has not been built or run** — there was no Docker available
where this was written. If `up --build` fails, that is why, and the failure is
in this directory rather than in the stack.
