"""The task entrypoint, and the one thing about it that is not obvious.

A serverless `spark_python_task` runs inside an **ipykernel**, which treats a
`SystemExit` as an exception rather than as an exit. So `sys.exit(0)` — the
ordinary, correct ending for a CLI — fails the task:

    SystemExit: 0
    An exception has occurred, use %tb to see the full traceback.
    ... Workload failed, see run output for details

That is not a hypothesis. A deployed heartbeat on 2026-09-03 emitted all 65
messages, wrote three part files to the volume and recorded a terminal
SUCCEEDED — and Databricks reported the task as RUN_EXECUTION_ERROR, with
`SystemExit: 0` as the only clue.

`runpy.run_path(..., run_name="__main__")` reproduces it faithfully, because it
does what the kernel does: run the file and let `SystemExit` propagate to the
caller instead of ending the process. Nothing else offline catches this.
"""

from __future__ import annotations

import json
import pathlib
import runpy
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
ENTRYPOINT = ROOT / "job" / "run_model.py"


@pytest.fixture
def task_args(tmp_path, monkeypatch):
    """The arguments a job task passes, as `KEY=VALUE` positionals.

    Serverless tasks have no `spark_env_vars`, which is why they arrive this
    way and why `run_model.main` translates them at all.
    """
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(ENTRYPOINT),
            "DBX_RUN_ID=exit-code-check",
            "DBX_MODEL=heartbeat",
            'DBX_MODEL_CONFIG={"seconds": 0.2, "hz": 10}',
            f"DBX_TELEMETRY_VOLUME={tmp_path}",
            # Empty on purpose: no app, so the run goes durable-only. That is
            # the normal case, and it keeps this test off the network.
            "DBX_APP_URL=",
        ],
    )
    return tmp_path


def test_a_successful_run_does_not_raise_systemexit(task_args):
    """The regression. `sys.exit(0)` here fails the Databricks task.

    Note what is being asserted: not an exit CODE, but the absence of a raised
    `SystemExit`. In an ordinary process the two are indistinguishable — which
    is exactly why this shipped.
    """
    try:
        runpy.run_path(str(ENTRYPOINT), run_name="__main__")
    except SystemExit as exc:
        pytest.fail(
            f"the entrypoint raised SystemExit({exc.code!r}) on a successful run. "
            f"Inside a serverless spark_python_task's ipykernel that is reported "
            f"as a failed workload, however well the run went."
        )


def test_the_run_really_happened(task_args):
    """Guards the test above from passing for the wrong reason.

    A no-op entrypoint would also raise no SystemExit. The durable record is
    what distinguishes "succeeded quietly" from "did nothing".
    """
    runpy.run_path(str(ENTRYPOINT), run_name="__main__")

    parts = sorted((task_args / "runs" / "exit-code-check").glob("part-*.jsonl"))
    assert parts, "no part files: the entrypoint returned without running anything"

    records = [
        json.loads(line) for part in parts for line in part.read_text().splitlines() if line.strip()
    ]
    terminal = [r for r in records if r["type"] == "status" and r.get("terminal")]

    assert len(terminal) == 1, "a run must end with exactly one terminal status"
    assert terminal[0]["status"] == "SUCCEEDED"


def test_a_failed_run_raises_something_that_says_why(tmp_path, monkeypatch):
    """The other half, and it must NOT be silenced.

    A failed run has to fail the task, or a scheduled job reports success
    having produced nothing and neither retries nor alerting fire.

    It raises rather than calling `sys.exit(1)`: inside the kernel both are
    exceptions, so the only difference is what the run output says, and
    `SystemExit: 1` names nothing.
    """
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(ENTRYPOINT),
            "DBX_RUN_ID=failure-check",
            "DBX_MODEL=not_a_real_model_anywhere",
            f"DBX_TELEMETRY_VOLUME={tmp_path}",
            "DBX_APP_URL=",
        ],
    )

    with pytest.raises(Exception) as exc:
        runpy.run_path(str(ENTRYPOINT), run_name="__main__")

    assert not isinstance(exc.value, SystemExit), (
        "SystemExit says nothing useful in a run output; raise something that does"
    )
    assert "did not succeed" in str(exc.value)


def test_empty_arguments_are_dropped_rather_than_exported(monkeypatch):
    """`DBX_APP_URL=` must mean "no app", not "an app at the empty string".

    The job YAML defaults it to empty, so this is the common path rather than
    an edge case: an empty string here would send the harness looking for an
    app at `wss:///ws/job/...`.
    """
    monkeypatch.delenv("DBX_APP_URL", raising=False)
    monkeypatch.setenv("DBX_KEEP_ME", "before")

    from job.run_model import main

    # A model that does not exist, so `main` returns non-zero without running;
    # the assertion is about the environment it set up on the way.
    main(["DBX_APP_URL=", "DBX_KEEP_ME=after", "DBX_MODEL=not_a_real_model_anywhere"])

    import os

    assert "DBX_APP_URL" not in os.environ
    assert os.environ["DBX_KEEP_ME"] == "after"
