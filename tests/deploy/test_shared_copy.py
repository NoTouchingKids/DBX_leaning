"""`app/shared/` must be a faithful copy of `shared/`.

`app/` is deployed on its own — `resources/app.yml` gives Databricks Apps that
folder as its `source_code_path`, and nothing outside it travels. But
`app/server/` imports `shared`, the message envelope that `job/` and
`models/` import too, so a copy has to be inside the folder for the deployed
process to start at all. It cannot be a symlink: the workspace export rejects
those, which is the failure that started this whole line of work.

So one directory is canonical (`shared/`, imported by everything that is not
the deployed app) and one is a copy (`app/shared/`, tracked rather than
generated, because a deploy driven from inside Databricks sees only tracked
files).

Duplication's failure mode is drift, and drift here is nasty in a specific
way: the tests would pass against the canonical copy while the deployed app
ran the stale one, so the envelope contract would hold everywhere except in
production. That is what these tests exist to make impossible.

The duplication is scoped to this stage. Packaging `shared` as a wheel
retires both the copy and this file.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import sync_shared  # noqa: E402


def test_the_copy_matches_the_canonical_shared():
    drift = sync_shared.differences()
    assert drift == [], (
        "app/shared has drifted from shared/: "
        + ", ".join(drift)
        + " — run `uv run python scripts/sync_shared.py`"
    )


def test_the_copy_exists_at_all():
    """A missing copy is not a drift report, it is a deploy that cannot boot:
    `server/main.py` imports `shared.envelope` on the first line it runs."""
    assert (ROOT / "app" / "shared" / "envelope.py").is_file()


def test_the_copy_is_tracked_by_git():
    """Not generated at deploy time. A deploy driven from inside Databricks —
    a Git folder, a notebook — sees only tracked files."""
    import subprocess

    tracked = subprocess.run(
        ["git", "ls-files", "app/shared"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert "app/shared/envelope.py" in tracked


def test_the_copy_is_not_a_symlink():
    """The one thing the workspace export cannot handle."""
    symlinks = [
        p for p in (ROOT / "app").rglob("*") if p.is_symlink() and "node_modules" not in p.parts
    ]
    assert symlinks == [], f"the app folder cannot contain symlinks: {symlinks}"


def test_imports_resolve_to_the_canonical_shared_not_the_copy():
    """`pythonpath` in pyproject.toml puts the repo root ahead of `app/`.

    With the order reversed every test in this suite would exercise the COPY,
    and `test_the_copy_matches_the_canonical_shared` would be the only thing
    standing between that and a silent divergence. Assert the order directly.
    """
    import shared

    assert pathlib.Path(shared.__file__).parent == ROOT / "shared", (
        "tests are importing app/shared, the deployed copy, not shared/"
    )


@pytest.mark.parametrize("name", ["envelope", "protocol", "schema", "tables", "codec", "seq"])
def test_every_module_the_app_imports_is_in_the_copy(name):
    assert (ROOT / "app" / "shared" / f"{name}.py").is_file()
