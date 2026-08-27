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
