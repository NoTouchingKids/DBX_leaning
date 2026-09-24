"""The notebook is documentation that runs, so it is tested like code.

`notebooks/heartbeat.py` is the answer to "how do I work on a model from a
notebook", and the answer stops being true the moment an import moves or
`run_local`'s signature changes. A notebook nobody executes is the most
confident kind of stale documentation — it LOOKS authoritative.

Databricks notebook source is valid Python with `# COMMAND ----------` cell
separators and `# MAGIC` prefixes for non-Python cells, so the code cells can
simply be run. Two kinds are skipped, and only two:

  * `%pip install ...` — a workspace operation with no local meaning
  * anything touching `dbutils` — injected by the Databricks runtime

Whatever those cells claim is NOT covered here. That gap is real and named at
the top of the notebook itself.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
NOTEBOOK = ROOT / "notebooks" / "heartbeat.py"


HEADER = "# Databricks notebook source"


def _cells() -> list[str]:
    src = NOTEBOOK.read_text(encoding="utf-8")
    # The header belongs to the FILE, not to the first cell. Leaving it on
    # makes cell one fail a `startswith("# MAGIC")` check and get classified as
    # code — harmless when it is all comments, and wrong the moment it is not.
    src = src.replace(HEADER, "", 1)
    return [c.strip() for c in src.split("# COMMAND ----------")]


def code_cells() -> list[str]:
    """Cells a person could actually run here.

    A cell counts as code when it has a line that is neither blank nor a
    comment — `# MAGIC` markdown is all comments, so this needs no special
    case. `dbutils` cells are the Databricks runtime's and are excluded.
    """
    out = []
    for cell in _cells():
        if not cell or "dbutils" in cell:
            continue
        if any(line.strip() and not line.lstrip().startswith("#") for line in cell.splitlines()):
            out.append(cell)
    return out


def test_the_notebook_is_databricks_notebook_source():
    """The first line is what makes the workspace import it as a NOTEBOOK
    rather than a file. Without it you get a Python file you cannot run cells
    in, which looks like the notebook being broken."""
    first = NOTEBOOK.read_text(encoding="utf-8").splitlines()[0]
    assert first == "# Databricks notebook source", first


def test_every_code_cell_runs():
    """One process, cells in order, exactly as a person would run them.

    `seconds=` is dialled down first: the notebook uses durations a human
    watching would want, and this should not add ten seconds to the suite to
    re-prove that `time.sleep` works. The imports, the API and the shape of
    what comes back are what is under test.
    """
    cells = code_cells()
    assert len(cells) >= 5, f"only {len(cells)} runnable cells; did the format change?"

    script = "\n\n".join(cells)
    script = re.sub(r"seconds=\d+", "seconds=0.3", script)
    script = re.sub(r"hz=\d+", "hz=10", script)
    # No volume in a test environment, and pointing at one would make this a
    # Databricks test rather than a notebook test.
    script = script.replace('"/Volumes/main/dbx_leaning/telemetry"', "None")

    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=ROOT,
    )
    assert result.returncode == 0, (
        f"a notebook cell failed:\n--- stdout ---\n{result.stdout[-2000:]}\n"
        f"--- stderr ---\n{result.stderr[-3000:]}"
    )
    assert "SUCCEEDED" in result.stdout


@pytest.mark.parametrize("name", ["heartbeat", "job.local", "job.loader"])
def test_the_notebook_imports_what_it_says_it_does(name: str):
    """The notebook's claim is that these are ordinary installed packages.

    If one of them ever needs a `sys.path` line to import, the notebook is
    wrong and this is the cheapest place to notice — the whole restructure
    exists so that a notebook needs no path machinery.
    """
    source = NOTEBOOK.read_text(encoding="utf-8")
    assert f"import {name.split('.')[0]}" in source or f"from {name} import" in source


def test_the_notebook_arranges_no_paths():
    """The point of the exercise, asserted directly.

    `sys.path.append`, a repo-root search, or a `%run` of another notebook
    would all mean the packaging did not actually solve this — which is the
    complaint that started the restructure.

    CODE cells only. Checking the whole file fails on the prose in cell one,
    which names these in order to say the notebook does not need them — a
    grep that cannot tell "does X" from "explains why X is unnecessary" would
    make the documentation unwritable.
    """
    for cell in code_cells():
        for smell in ("sys.path", "%run", "repo_root", "os.chdir"):
            assert smell not in cell, f"a notebook cell resorts to {smell!r}:\n{cell}"
