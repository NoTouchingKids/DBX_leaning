#!/usr/bin/env bash
#
# The whole pre-deploy, in one command:
#
#     scripts/deploy.sh -t dev
#
# Everything after the script name is passed straight through to
# `databricks bundle deploy`.
#
# Why this exists
# ---------------
#
# Two of the things the bundle deploys are BUILD OUTPUT, and both are
# gitignored — correctly, they are generated:
#
#   frontend/dist/     the SPA that `app/spa.py` serves
#   build/app_source/  the symlink-free app folder the App export can accept
#
# So a fresh clone has neither, and `databricks bundle deploy` fails on the
# second one with:
#
#     Error: stat build/app_source: no such file or directory
#
# which is accurate and says nothing about what to do. Rather than document a
# three-command ritual and rely on everyone remembering it, run the ritual.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# The frontend is skippable because it is the slow step and is often already
# built. It is never silently skipped: stage_app.py warns loudly when there is
# no dist/, and the app answers 503 on every page rather than pretending.
if [[ "${DBX_SKIP_FRONTEND:-}" == "1" ]]; then
  echo "==> skipping frontend build (DBX_SKIP_FRONTEND=1)"
elif ! command -v pnpm >/dev/null 2>&1; then
  echo "==> pnpm not found; skipping frontend build" >&2
else
  echo "==> building frontend"
  (cd frontend && pnpm install --frozen-lockfile && pnpm build)
fi

echo "==> staging app source"
uv run python scripts/stage_app.py

echo "==> validating bundle"
databricks bundle validate "$@"

echo "==> deploying"
databricks bundle deploy "$@"
