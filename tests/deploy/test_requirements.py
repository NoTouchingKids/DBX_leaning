"""The generated requirements files, and the two ways they have broken.

Both failures are invisible locally and fatal on a deploy, which is the whole
reason they are worth a test rather than a habit.
"""

from __future__ import annotations

import pathlib
import tomllib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
GENERATED = (ROOT / "app" / "requirements.txt", ROOT / "job" / "requirements.txt")


@pytest.mark.parametrize("path", GENERATED, ids=lambda p: str(p.relative_to(ROOT)))
def test_a_generated_requirements_file_is_utf8(path: pathlib.Path):
    """`Path.write_text` with no encoding uses the LOCALE encoding.

    On Windows that is cp1252, and the generated header contains an em dash,
    so the file came out as:

        b'# GENERATED \\x97 do not edit'

    which is not valid UTF-8. uv refuses it outright — "failed to decode file
    ...: stream did not contain valid UTF-8" — and a deploy has no better
    outcome for being quieter about it.

    It stayed hidden because the file had been generated on Linux, where the
    default encoding is already UTF-8. It only appeared when someone
    regenerated it on Windows, and then only as a corrupt byte in a comment
    that reads fine at a glance.
    """
    raw = path.read_bytes()
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        window = raw[max(0, exc.start - 12) : exc.start + 12]
        pytest.fail(
            f"{path.relative_to(ROOT)} is not valid UTF-8 at byte {exc.start}: {window!r}. "
            f"scripts/export_requirements.py must pass encoding='utf-8' explicitly."
        )


def test_the_python_floor_admits_the_app_runtime():
    """A Databricks App with a `requirements.txt` is installed with PIP, and a
    pip-based app is **Python 3.11** — not a choice, a property of the
    platform. Only a uv-based app (pyproject.toml + uv.lock and NO
    requirements.txt) may pick its own version.

    Serverless jobs are 3.12 (environment_version 5), and one lockfile feeds
    both. So `requires-python` has to admit 3.11, or `app/requirements.txt` is
    a resolution the app's interpreter may be unable to install.

    `databricks environments setup-local --serverless-version 5` writes
    `==3.12.*` here, which is right for the jobs and wrong for the app. If a
    future run of it resets the line, this is what says why.
    """
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    floor = pyproject["project"]["requires-python"]

    assert "3.12.*" not in floor, (
        f"requires-python is {floor!r}, which excludes Python 3.11 — the version "
        f"a pip-based Databricks App runs on. Re-run of `setup-local`?"
    )
    assert floor.startswith(">=3.11"), floor


def test_nothing_in_the_runtime_dependency_sets_requires_3_12():
    """A dependency that needs 3.12 makes the app unresolvable, and the error
    names the dependency rather than the app.

    `databricks-connect` did exactly this — `setup-local` adds it, it requires
    Python >=3.12,<3.13, and it made the whole workspace unsatisfiable the
    moment the floor dropped to 3.11. It is confined to 3.12 by a marker and
    lives in the DEV group, where nothing deployable can reach it.
    """
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    runtime = pyproject["project"]["dependencies"] + [
        dep for extra in pyproject["project"]["optional-dependencies"].values() for dep in extra
    ]
    assert not any("databricks-connect" in dep for dep in runtime), (
        "databricks-connect requires Python >=3.12 and would exclude the app's "
        "3.11 runtime; it belongs in the dev group with a marker"
    )
