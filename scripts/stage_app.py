#!/usr/bin/env python3
"""Assemble exactly what the Databricks App needs, and nothing else.

Run before ``databricks bundle deploy``::

    uv run python scripts/stage_app.py

Why this exists
---------------

``resources/app.yml`` used to set ``source_code_path: ../`` — the whole repo.
The App deployment exports that folder, and the export **cannot handle
symlinks**::

    Failed to export .../DBX_leaning/.venv/bin/python
    INVALID_PARAMETER_VALUE: Path (...) is not an exportable asset. type=symlink

``.venv`` is the one that fails first, but it is not the real problem.
``frontend/node_modules`` contains **thousands** of symlinks, because that is
how pnpm stores packages — so removing ``.venv`` just moves the failure. The
repo root is structurally not exportable and never will be.

It is also enormous. The app needs four things; the repo root carries eleven
model packages, a job harness, a frontend source tree, tests, docs and two
dependency trees.

So the app gets its own directory, containing only:

- ``app/``            the FastAPI application
- ``shared/``         the envelope, which ``app/`` imports
- ``requirements.txt``  where Databricks Apps looks for it, at the app root
- ``static/``         the built frontend, which ``app/spa.py`` serves

Nothing else in the repo is reachable from ``app/`` — verified by
``tests/deploy/test_app_source.py``, which walks the import graph rather than
trusting this list.

The output is generated, gitignored, and reproducible: delete it and run this
again. It is named in ``databricks.yml``'s ``sync.include`` for the same
reason ``frontend/dist`` is — git is right to ignore a build artefact, and
the deploy still needs it.
"""

from __future__ import annotations

import argparse
import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
STAGE = ROOT / "build" / "app_source"

#: Copied wholesale. Keep this in step with the import graph, not with taste —
#: `tests/deploy/test_app_source.py` fails if `app/` reaches something absent.
PACKAGES = ("app", "shared")

#: The built SPA, and where it lands inside the staged app. `app/config.py`
#: resolves a relative `DBX_FRONTEND_DIST` against the app root, so `static`
#: works there and `frontend/dist` would not exist.
FRONTEND_DIST = ROOT / "frontend" / "dist"
FRONTEND_TARGET = "static"


def _ignore(_dir: str, names: list[str]) -> set[str]:
    """Drop what should never travel, symlinks above all.

    A symlink is not merely unwanted here: the workspace export rejects the
    whole deployment when it meets one, naming a single file, which reads as a
    problem with that file rather than with the whole approach.
    """
    dropped = set()
    for name in names:
        if name in {"__pycache__", ".pytest_cache", ".ruff_cache"}:
            dropped.add(name)
        elif name.endswith((".pyc", ".pyo")):
            dropped.add(name)
        elif (pathlib.Path(_dir) / name).is_symlink():
            dropped.add(name)
    return dropped


def stage(*, quiet: bool = False) -> pathlib.Path:
    if STAGE.exists():
        shutil.rmtree(STAGE)
    STAGE.mkdir(parents=True)

    for package in PACKAGES:
        shutil.copytree(ROOT / package, STAGE / package, ignore=_ignore, symlinks=False)

    shutil.copy2(ROOT / "requirements.txt", STAGE / "requirements.txt")

    if FRONTEND_DIST.is_dir():
        shutil.copytree(FRONTEND_DIST, STAGE / FRONTEND_TARGET, ignore=_ignore, symlinks=False)
    else:
        # Not fatal — the API is unaffected and `app/spa.py` answers 503 with a
        # message saying so. Loud, because a deploy that silently serves no UI
        # is the thing that wastes an afternoon.
        print(
            f"WARNING: no built frontend at {FRONTEND_DIST}. The app will serve "
            f"503 on every page. Run `pnpm build` in frontend/ first.",
            file=sys.stderr,
        )

    # The failure this whole file exists to prevent. Assert it rather than
    # assume the ignore function was complete.
    symlinks = [p for p in STAGE.rglob("*") if p.is_symlink()]
    if symlinks:
        raise SystemExit(
            f"staged app still contains symlinks, which cannot be exported: {symlinks}"
        )

    if not quiet:
        files = sum(1 for p in STAGE.rglob("*") if p.is_file())
        size = sum(p.stat().st_size for p in STAGE.rglob("*") if p.is_file())
        print(f"staged {files} files ({size / 1_048_576:.1f} MB) into {STAGE.relative_to(ROOT)}")
    return STAGE


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify a staged app exists and is symlink-free, without rebuilding",
    )
    args = parser.parse_args()

    if args.check:
        if not (STAGE / "app" / "main.py").is_file():
            print(f"no staged app at {STAGE}; run scripts/stage_app.py", file=sys.stderr)
            return 1
        symlinks = [p for p in STAGE.rglob("*") if p.is_symlink()]
        if symlinks:
            print(f"staged app contains symlinks: {symlinks}", file=sys.stderr)
            return 1
        print("staged app looks deployable")
        return 0

    stage(quiet=args.quiet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
