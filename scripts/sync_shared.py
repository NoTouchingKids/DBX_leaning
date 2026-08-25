#!/usr/bin/env python3
"""Copy ``shared/`` into each deployable folder, which must be self-contained.

    uv run python scripts/sync_shared.py          # refresh the copies
    uv run python scripts/sync_shared.py --check  # are they current? (what the test does)

Why a copy exists at all
------------------------

Each deployable unit is a folder that carries everything it needs:

- ``app/`` is what ``resources/app.yml`` gives Databricks Apps as its
  ``source_code_path``. It is exported and deployed on its own and nothing
  outside it travels — but ``app/server/`` imports ``shared``, the message
  envelope. Without a copy inside the folder the deployed process does not
  start. **This copy is load-bearing today.**
- ``job/`` is the job unit: the harness, ``job/models/``, and its own
  requirements. **This copy is not load-bearing today** — a job task runs
  ``entrypoints/run_model.py`` out of the whole synced repo tree, so it
  imports the canonical ``shared`` and never reads ``job/shared/``. It is here
  so the folder is already a complete unit when it is packaged as a wheel or
  deployed alone, which is where test and prod are going.

Neither can be a symlink: the workspace export rejects those outright, which
is the failure that started this whole line of work.

So one directory is canonical and the rest are copies:

- ``shared/`` at the repo root is the source of truth. ``job/``,
  ``job/models/``, ``scripts/`` and ``tests/`` import it, unchanged.
- ``app/shared/`` and ``job/shared/`` are byte-identical copies, TRACKED in
  git rather than generated at deploy time — because a deploy driven from
  inside Databricks sees only tracked files, so a gitignored copy would
  simply not be there.

The copies are a known compromise, scoped to this stage. Packaging ``shared``
as a wheel retires this file and both duplicate directories.

**Drift is the whole risk**, so it is not left to discipline:
``tests/deploy/test_shared_copy.py`` fails the moment any copy differs, and
names this command as the fix. Editing a copy directly is the one thing that
will not work — it is overwritten from the root on every sync.
"""

from __future__ import annotations

import argparse
import filecmp
import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCE = ROOT / "shared"

#: Every folder that has to carry its own copy. `app/` needs one to boot;
#: `job/` carries one so it is a complete unit when it is packaged.
TARGETS = (ROOT / "app" / "shared", ROOT / "job" / "shared")

#: Never copied. `__pycache__` in particular would be a stale-bytecode landmine
#: in a deployed folder, and its presence makes the two trees differ forever.
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", ".pytest_cache")

#: Written into each copy so anyone who opens it knows not to edit it.
#: Excluded from the comparison below — it has no counterpart in the source,
#: and counting it would report permanent drift.
MARKER = "GENERATED"

HEADER = """GENERATED — do not edit. A copy of the repo root's shared/, kept here
because this folder is a deployable unit and has to carry what it imports.
Edits here are overwritten. Change shared/ instead, then run:

    uv run python scripts/sync_shared.py
"""


def _files(root: pathlib.Path) -> set[str]:
    return {
        str(p.relative_to(root))
        for p in root.rglob("*")
        if p.is_file()
        and p.name != MARKER
        and "__pycache__" not in p.parts
        and p.suffix not in {".pyc", ".pyo"}
    }


def differences(target: pathlib.Path) -> list[str]:
    """Every path in `target` that differs from the source. Empty means in sync."""
    if not target.is_dir():
        return sorted(_files(SOURCE))

    source_files, target_files = _files(SOURCE), _files(target)
    # A file the copy has and the source does not is drift too — usually a
    # module deleted upstream, which would keep importing inside the deployed
    # unit and nowhere else, so it would work in a way nothing else does.
    diff = source_files ^ target_files
    for rel in source_files & target_files:
        if not filecmp.cmp(SOURCE / rel, target / rel, shallow=False):
            diff.add(rel)
    return sorted(diff)


def all_differences() -> dict[pathlib.Path, list[str]]:
    """Drift per target, listing only the targets that have any."""
    found = {t: differences(t) for t in TARGETS}
    return {t: d for t, d in found.items() if d}


def sync() -> None:
    for target in TARGETS:
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(SOURCE, target, ignore=IGNORE, symlinks=False)
        (target / MARKER).write_text(HEADER)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="report drift and exit non-zero, without writing anything",
    )
    args = parser.parse_args()

    if args.check:
        drift = all_differences()
        if drift:
            for target, paths in drift.items():
                print(
                    f"{target.relative_to(ROOT)} is {len(paths)} file(s) out of date with shared/:",
                    file=sys.stderr,
                )
                for rel in paths:
                    print(f"  {rel}", file=sys.stderr)
            print("run: uv run python scripts/sync_shared.py", file=sys.stderr)
            return 1
        names = ", ".join(str(t.relative_to(ROOT)) for t in TARGETS)
        print(f"in sync with shared/: {names}")
        return 0

    sync()
    count = len(_files(SOURCE))
    for target in TARGETS:
        print(f"copied {count} files from shared/ to {target.relative_to(ROOT)}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
