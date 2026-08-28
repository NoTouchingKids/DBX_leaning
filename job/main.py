"""Job entrypoint. One process, one run.

A small singleton created here is fine — the "no module-level globals holding
live objects" rule is about the app, where a request can arrive before or
after a service exists. Here, the process *is* the run.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import signal
import sys
from collections.abc import Coroutine
from typing import Any

from .config import JobConfig
from .runner import JobHarness
from .shared.envelope import RunStatus

log = logging.getLogger("job")


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


#: Failures a live channel is *expected* to produce when the app is down —
#: which is a normal state here, not an error.
_TRANSPORT_ERRORS = (EOFError, ConnectionError, OSError, asyncio.InvalidStateError)


def _install_loop_error_handler(loop: asyncio.AbstractEventLoop) -> None:
    """Stop a dead live channel from printing a stack trace at ERROR.

    A websocket that cannot reach the app raises inside the transport's own
    callbacks, where asyncio's default handler logs it at ERROR with a full
    traceback. On a run that succeeded with nothing listening, that reads like
    a failure — and "nothing listening" is the normal case, because apps run
    ~8h/day and jobs do not. Real errors still go to the default handler.
    """
    default = loop.get_exception_handler()

    def handler(loop: asyncio.AbstractEventLoop, context: dict) -> None:
        exc = context.get("exception")
        if isinstance(exc, _TRANSPORT_ERRORS):
            log.info("live channel transport gave up: %s (the run is unaffected)", exc)
            return
        if default is not None:
            default(loop, context)
        else:
            loop.default_exception_handler(context)

    loop.set_exception_handler(handler)


async def _amain() -> int:
    cfg = JobConfig.from_env()
    harness = JobHarness(cfg)

    loop = asyncio.get_running_loop()
    _install_loop_error_handler(loop)

    for sig in (signal.SIGTERM, signal.SIGINT):
        # Databricks cancels a task with SIGTERM. Treating it as a cancel
        # rather than a kill is what lets results already produced survive.
        try:
            loop.add_signal_handler(sig, lambda s=sig: harness.token.cancel(f"received {s.name}"))
        except (NotImplementedError, RuntimeError, ValueError) as exc:
            # NotImplementedError on non-POSIX. RuntimeError when this loop is
            # not on the main thread — "set_wakeup_fd only works in main thread
            # of the main interpreter" — which is exactly where `_run` below
            # puts us on serverless, since the kernel owns the main thread.
            #
            # Not fatal, and not silent either: cancel from the UI travels over
            # the WebSocket control channel and is unaffected. What is lost is
            # the SIGTERM path — `databricks jobs cancel-run` and the platform's
            # own task cancellation become a kill rather than a graceful stop,
            # so a run cancelled that way keeps only what it had already
            # flushed instead of writing out its incumbent.
            log.info("no %s handler (%s); cancel over the WebSocket still works", sig.name, exc)

    outcome = await harness.run()
    log.info(
        "run %s finished: %s (seq=%d rows_written=%d results=%d chunks=%d "
        "live_sent=%d live_dropped=%d undrained=%d backfills=%d status_reports=%d "
        "observed=%s)",
        outcome.run_id,
        outcome.status.value,
        outcome.seq_issued,
        outcome.rows_written,
        outcome.result_rows,
        outcome.result_chunks,
        outcome.live_sent,
        outcome.live_dropped,
        outcome.live_undrained,
        outcome.backfills_served,
        outcome.status_reports,
        outcome.observed_live,
    )
    if outcome.detail:
        log.info("detail: %s", outcome.detail)
    return 0 if outcome.status in (RunStatus.SUCCEEDED, RunStatus.CANCELLED) else 1


def _run(coro: Coroutine[Any, Any, int]) -> int:
    """Drive the run to completion, with or without a loop already running.

    `asyncio.run` is right for the normal case — this process exists to do one
    run and then exit. But a serverless `spark_python_task` does not give us a
    process: it reads this repo's entrypoint and `exec`s it inside an
    ipykernel, which is already running a loop of its own, and `asyncio.run`
    refuses outright:

        RuntimeError: asyncio.run() cannot be called from a running event loop

    So when there is a loop, the run gets its own on a worker thread. The
    kernel's loop keeps turning, ours is a normal `asyncio.run` that happens
    not to be on the main thread, and `.result()` blocks the caller until the
    run finishes — which is the semantics the task runner expects anyway.

    `nest_asyncio` would be the other answer. It is a dependency that
    monkey-patches the event loop, and it would have to be added to all eleven
    model environments to fix a problem in one of them.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    log.info("an event loop is already running here; taking a thread of our own")
    with concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="job") as pool:
        return pool.submit(asyncio.run, coro).result()


def main() -> int:
    _setup_logging()
    try:
        return _run(_amain())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
