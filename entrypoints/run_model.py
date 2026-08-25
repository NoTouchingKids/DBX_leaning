"""Databricks Job entrypoint. One of these runs every model.

Why this file exists at all: with workspace-files sync the job runs a *file*,
not an installed package, so something has to put the synced repo root on
``sys.path`` before ``job.main`` can be imported. And serverless tasks have no
``spark_env_vars``, so job parameters arrive as command-line arguments while
``job/config.py`` reads the environment — this bridges the two.

Deliberately thin. Anything with logic in it belongs in ``job/``, where it can
be tested without a workspace.

    run_model.py DBX_RUN_ID=abc DBX_MODEL=job.models.scenario DBX_APP_URL=

An argument with an empty value is dropped rather than exported as an empty
string, so an unset ``DBX_APP_URL`` means "no app" rather than "an app at ''".
"""

from __future__ import annotations

import os
import pathlib
import sys

#: The synced repo root: <root>/entrypoints/run_model.py -> <root>
ROOT = pathlib.Path(__file__).resolve().parent.parent


def parse_settings(args: list[str]) -> dict[str, str]:
    """``KEY=VALUE`` arguments to an environment mapping.

    Rejects anything malformed rather than ignoring it: a typo'd parameter
    that silently does nothing would show up as a run that ignored its own
    configuration, which is a miserable thing to debug from a job log.
    """
    settings: dict[str, str] = {}
    for arg in args:
        if "=" not in arg:
            raise SystemExit(
                f"bad argument {arg!r}: expected KEY=VALUE\n"
                f"  usage: run_model.py DBX_RUN_ID=... DBX_MODEL=job.models.scenario ..."
            )
        key, _, value = arg.partition("=")
        key = key.strip()
        if not key:
            raise SystemExit(f"bad argument {arg!r}: empty key")
        if value != "":
            settings[key] = value
    return settings


def main(argv: list[str] | None = None) -> int:
    settings = parse_settings(list(sys.argv[1:] if argv is None else argv))
    os.environ.update(settings)

    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    from job.main import main as run  # imported after sys.path is ready

    return run()


if __name__ == "__main__":
    sys.exit(main())
