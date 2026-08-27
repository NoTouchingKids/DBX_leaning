"""The process entrypoint: signals, exit codes, and log noise."""

from __future__ import annotations

import asyncio
import logging

import pytest

from job.main import _install_loop_error_handler


async def test_a_dead_live_channel_does_not_log_at_error(caplog):
    """ "Nothing listening" is the normal case. A stack trace at ERROR on a
    run that succeeded reads like a failure."""
    loop = asyncio.get_running_loop()
    _install_loop_error_handler(loop)

    with caplog.at_level(logging.DEBUG):
        loop.call_exception_handler(
            {"message": "transport died", "exception": ConnectionRefusedError("app is down")}
        )

    records = [r for r in caplog.records if "transport gave up" in r.message]
    assert records and records[0].levelno == logging.INFO
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


async def test_a_real_error_still_reaches_the_default_handler(caplog):
    loop = asyncio.get_running_loop()
    _install_loop_error_handler(loop)

    with caplog.at_level(logging.DEBUG):
        loop.call_exception_handler(
            {"message": "something actually broke", "exception": ValueError("a real bug")}
        )

    assert [r for r in caplog.records if r.levelno >= logging.ERROR]


@pytest.mark.parametrize(
    "status,expected_exit",
    [("SUCCEEDED", 0), ("CANCELLED", 0), ("FAILED", 1), ("INFEASIBLE", 1)],
)
async def test_exit_code_treats_cancellation_as_a_clean_outcome(
    monkeypatch, tmp_path, status, expected_exit
):
    from job import main as main_module
    from job.runner import RunOutcome
    from job.shared.envelope import RunStatus

    monkeypatch.setenv("DBX_MODEL", "job.models.scenario")
    monkeypatch.setenv("DBX_WRITER", "jsonl")
    monkeypatch.setenv("DBX_LOCAL_ROOT", str(tmp_path))

    async def fake_run(self):
        return RunOutcome(run_id="r", status=RunStatus(status), detail="test")

    monkeypatch.setattr(main_module.JobHarness, "run", fake_run)
    assert await main_module._amain() == expected_exit


class TestRunningWhereALoopAlreadyExists:
    """A serverless `spark_python_task` does not give the job a process.

    It reads `job/run_model.py` and `exec`s it inside an ipykernel that is
    already running a loop of its own, so `main()`'s `asyncio.run` refused
    outright and every run on a real workspace ended:

        RuntimeError: asyncio.run() cannot be called from a running event loop

    These tests are `async def`, which is the point — pytest-asyncio runs them
    with a loop already going, exactly the condition that broke it. The old
    code fails all of them.
    """

    async def test_main_completes_from_inside_a_running_loop(self, monkeypatch, tmp_path):
        from job import main as main_module
        from job.runner import RunOutcome
        from job.shared.envelope import RunStatus

        asyncio.get_running_loop()  # the precondition, stated rather than assumed

        monkeypatch.setenv("DBX_MODEL", "job.models.scenario")
        monkeypatch.setenv("DBX_WRITER", "jsonl")
        monkeypatch.setenv("DBX_LOCAL_ROOT", str(tmp_path))

        async def fake_run(self):
            return RunOutcome(run_id="r", status=RunStatus.SUCCEEDED)

        monkeypatch.setattr(main_module.JobHarness, "run", fake_run)
        assert main_module.main() == 0

    async def test_the_missing_signal_handler_is_reported_not_raised(
        self, monkeypatch, tmp_path, caplog
    ):
        """Fixing `asyncio.run` alone moved the crash one line down.

        `loop.add_signal_handler` raises `RuntimeError: set_wakeup_fd only
        works in main thread of the main interpreter` off the main thread, and
        the handler only caught `NotImplementedError`.
        """
        from job import main as main_module
        from job.runner import RunOutcome
        from job.shared.envelope import RunStatus

        monkeypatch.setenv("DBX_MODEL", "job.models.scenario")
        monkeypatch.setenv("DBX_WRITER", "jsonl")
        monkeypatch.setenv("DBX_LOCAL_ROOT", str(tmp_path))

        async def fake_run(self):
            return RunOutcome(run_id="r", status=RunStatus.SUCCEEDED)

        monkeypatch.setattr(main_module.JobHarness, "run", fake_run)

        with caplog.at_level(logging.INFO, logger="job"):
            assert main_module.main() == 0

        assert any("cancel over the WebSocket still works" in r.message for r in caplog.records), (
            "a lost SIGTERM handler must be said out loud — cancellation "
            "degrades from graceful to a kill"
        )

    def test_a_plain_process_still_uses_asyncio_run(self, monkeypatch, tmp_path):
        """The normal case must not regress into always spawning a thread."""
        from job import main as main_module
        from job.runner import RunOutcome
        from job.shared.envelope import RunStatus

        monkeypatch.setenv("DBX_MODEL", "job.models.scenario")
        monkeypatch.setenv("DBX_WRITER", "jsonl")
        monkeypatch.setenv("DBX_LOCAL_ROOT", str(tmp_path))

        async def fake_run(self):
            return RunOutcome(run_id="r", status=RunStatus.SUCCEEDED)

        monkeypatch.setattr(main_module.JobHarness, "run", fake_run)

        spawned = []
        real_pool = main_module.concurrent.futures.ThreadPoolExecutor

        def spy(*args, **kwargs):
            spawned.append(1)
            return real_pool(*args, **kwargs)

        monkeypatch.setattr(main_module.concurrent.futures, "ThreadPoolExecutor", spy)
        assert main_module.main() == 0
        assert spawned == [], "no loop is running here; asyncio.run is the right tool"
