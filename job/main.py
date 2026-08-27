"""Job entrypoint. One process, one run.

A small singleton created here is fine — the "no module-level globals holding
live objects" rule is about the app, where a request can arrive before or
after a service exists. Here, the process *is* the run.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

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
        except NotImplementedError:  # pragma: no cover - non-POSIX
            pass

    outcome = await harness.run()
    log.info(
        "run %s finished: %s (seq=%d rows_written=%d results=%d chunks=%d "
        "live_sent=%d live_dropped=%d observed=%s)",
        outcome.run_id,
        outcome.status.value,
        outcome.seq_issued,
        outcome.rows_written,
        outcome.result_rows,
        outcome.result_chunks,
        outcome.live_sent,
        outcome.live_dropped,
        outcome.observed_live,
    )
    if outcome.detail:
        log.info("detail: %s", outcome.detail)
    return 0 if outcome.status in (RunStatus.SUCCEEDED, RunStatus.CANCELLED) else 1


def main() -> int:
    _setup_logging()
    try:
        return asyncio.run(_amain())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
