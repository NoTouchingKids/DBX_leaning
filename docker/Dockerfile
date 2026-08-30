# The dev stack in a container: app + job launcher + the jobs it spawns.
#
# NOT a picture of the deployment. Databricks Apps builds the app from
# `app/requirements.txt` and a serverless task installs
# `deploy/requirements/<model>.txt` — neither uses this file, and nothing here
# should drift toward pretending otherwise. This exists so `docker compose up`
# gives you the same thing `scripts/dev_stack.py` gives you, on a machine that
# has neither uv nor the right Python, and beside a real Postgres.
#
# Python 3.11 to match `.python-version`, so the lock resolves the same set it
# does locally.
FROM python:3.11-slim

# curl for the healthcheck; git because uv reads the project as a workspace and
# some resolutions want it present. Nothing else — the models are pure Python
# or wheels.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl git \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /work

# Dependencies first, so editing source does not re-resolve. `--all-extras`
# because one container runs the app AND every model's job; the per-model
# split is a DEPLOYMENT property (one environment per job) and cannot be
# reproduced by a single container, so this deliberately installs the union.
# `deploy/requirements/*.txt` is what actually holds that split, and
# `tests/deploy/` is what checks it.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --all-extras --frozen --no-install-project

COPY . .

# job/shared/ is generated and gitignored, so a fresh checkout — or a COPY of
# one — cannot import `job` until this has run. The bundle's preinit hook does
# the same before every deploy.
RUN uv run python scripts/sync_shared.py \
 && uv sync --all-extras --frozen

# 8000 the app, 8787 the Jobs API substitute.
EXPOSE 8000 8787

# Bound to 0.0.0.0 rather than the script's 127.0.0.1 default: a port published
# out of a container has to be reachable from outside it.
ENV DBX_DEV_STACK_HOST=0.0.0.0

CMD ["uv", "run", "python", "scripts/dev_stack.py"]
