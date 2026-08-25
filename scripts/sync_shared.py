#!/usr/bin/env python3
"""Copy ``shared/`` into the app folder, which must be self-contained.

    uv run python scripts/sync_shared.py          # refresh the copy
    uv run python scripts/sync_shared.py --check  # is it current? (what the test does)

Why a copy exists at all
------------------------

``resources/app.yml`` gives Databricks Apps ``app/`` as its ``source_code_path``,
and that folder is exported and deployed on its own. Nothing outside it
travels. But ``app/server/`` imports ``shared`` — the message envelope, which
``job/`` and ``models/`` import too — so ``shared`` has to be *inside* ``app/``
for the deployed process to start at all.

It cannot be a symlink: the workspace export rejects those outright, which is
the failure that started this whole line of work.

So one directory is canonical and one is a copy:

- ``shared/`` at the repo root is the source of truth. ``job/``, ``models/``,
  ``scripts/`` and ``tests/`` import it, unchanged.
- ``app/shared/`` is a byte-identical copy, TRACKED in git rather than
  generated at deploy time — because a deploy driven from inside Databricks
  sees only tracked files, so a gitignored copy would simply not be there.

The copy is a known compromise, scoped to this stage. Test and prod are
expected to package ``shared`` as a wheel instead, at which point this file
and the duplicate directory both go away.

**Drift is the whole risk**, so it is not left to discipline:
``tests/deploy/test_shared_copy.py`` fails the moment the two differ, and
names this command as the fix. Editing the copy directly is the one thing
that will not work — it is overwritten from the root on every sync.
"""

from __future__ import annotations

import argparse
import filecmp
import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCE = ROOT / "shared"
TARGET = ROOT / "app" / "shared"

#: Never copied. `__pycache__` in particular would be a stale-bytecode landmine
#: in a deployed folder, and its presence makes the two trees differ forever.
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", ".pytest_cache")

#: Written into the copy so anyone who opens it knows not to edit it. Excluded
#: from the comparison below — it has no counterpart in the source, and
#: counting it would report permanent drift.
MARKER = "GENERATED"

HEADER = """GENERATED — do not edit. A copy of the repo root's shared/, kept here
because app/ is deployed on its own and app/server/ imports `shared`.
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


def differences() -> list[str]:
    """Every path that differs, as a sorted list. Empty means in sync."""
    if not TARGET.is_dir():
        return sorted(_files(SOURCE))

    source_files, target_files = _files(SOURCE), _files(TARGET)
    # A file the copy has and the source does not is drift too — usually a
    # module deleted upstream, which would keep importing here and nowhere
    # else, so the app would work in a way nothing else does.
    diff = source_files ^ target_files
    for rel in source_files & target_files:
        if not filecmp.cmp(SOURCE / rel, TARGET / rel, shallow=False):
            diff.add(rel)
    return sorted(diff)


def sync() -> None:
    if TARGET.exists():
        shutil.rmtree(TARGET)
    shutil.copytree(SOURCE, TARGET, ignore=IGNORE, symlinks=False)

    marker = TARGET / MARKER
    marker.write_text(HEADER)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="report drift and exit non-zero, without writing anything",
    )
    args = parser.parse_args()

    if args.check:
        drift = differences()
        if drift:
            print(
                f"app/shared is {len(drift)} file(s) out of date with shared/:",
                file=sys.stderr,
            )
            for rel in drift:
                print(f"  {rel}", file=sys.stderr)
            print("run: uv run python scripts/sync_shared.py", file=sys.stderr)
            return 1
        print("app/shared matches shared/")
        return 0

    sync()
    count = len(_files(TARGET))
    print(f"copied {count} files from shared/ to app/shared/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
