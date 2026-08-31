"""Job entrypoint. One process, one run, no event loop.

The v3 file this replaces was mostly a workaround. It ran the harness under
`asyncio.run`, discovered that a serverless `spark_python_task` executes inside
an ipykernel that already owns a running loop, and grew a ThreadPoolExecutor
plus twenty lines of explanation to nest one loop inside another. None of that
is here, because there is no loop to nest: the model blocks on this thread, the
telemetry roller and the socket each have one of their own.

What survives from v3 is the SIGTERM handling, and it survives because it earns
its place: Databricks cancels a task with SIGTERM, and treating that as a
cancel rather than a kill is what lets a run flush its telemetry and record an
honest terminal status instead of vanishing mid-part.
"""

from __future__ import annotations

import logging
import signal
import sys

from .config import JobConfig
from .harness import Harness
from .shared.envelope import RunStatus
from .telemetry import PartFileWriter
from .ws import RpcClient

log = logging.getLogger("job")


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


def _build_client(cfg: JobConfig, harness: Harness) -> RpcClient | None:
    """A live channel, if there is an app to talk to.

    No app is the normal case, not a degraded one — apps run ~8h/day and jobs
    do not. Returning None here is how "nobody is watching" is represented,
    and the run is identical either way apart from the commentary.
    """
    if not cfg.app_url or cfg.ws_url is None:
        log.info("no DBX_APP_URL — running unobserved, durable path only")
        return None

    from websockets.sync.client import connect

    from .auth import ingress_headers

    url = cfg.ws_url

    def headers() -> dict[str, str]:
        """Both credentials, fetched fresh per connection attempt.

        Two of them, on two different headers, and both are required — see
        `job/auth.py` for why `Authorization` cannot carry the shared secret.

        Fresh per attempt rather than once: the SDK caches and refreshes
        internally, and a reconnect an hour into a run must not present a token
        that expired forty minutes ago.
        """
        return ingress_headers(cfg.app_token, cfg.workspace_host)

    return RpcClient(
        url,
        cfg.run_id,
        connect=lambda: connect(url, additional_headers=headers() or None),
        on_cancel=lambda who: harness.cancel(who),
        on_replay=lambda a, b: harness.replay(a, b),
        next_seq=lambda: harness.seq.issued,
    )


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    cfg = JobConfig.from_env()

    writer = PartFileWriter(
        cfg.telemetry_root,
        cfg.run_id,
        max_bytes=cfg.flush_max_bytes,
        max_age_s=cfg.flush_max_age_s,
    )
    harness = Harness(
        cfg.run_id,
        writer,
        model_spec=cfg.model_spec,
        model_config=cfg.model_config,
        roll_tick_s=cfg.flush_tick_s,
    )

    client = _build_client(cfg, harness)
    if client is not None:
        harness._on_message = client.send  # noqa: SLF001 - assembled here on purpose
        client.start()

    # Databricks cancels a task with SIGTERM. Treating it as a cancel rather
    # than a kill is what lets the run flush its telemetry and record an honest
    # terminal status. Unlike v3 this cannot fail for being off the main
    # thread, because `main()` IS the main thread now.
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(
                sig, lambda s, _f: harness.token.cancel(f"received {signal.Signals(s).name}")
            )
        except (ValueError, OSError) as exc:
            # Only if something else already owns the handler. Cancel over the
            # RPC channel is unaffected; what is lost is the platform's own
            # task cancellation being graceful.
            log.info("no %s handler (%s); cancel over the socket still works", sig, exc)

    outcome = harness.run()

    if client is not None:
        client.stop()

    # `observed` is the CHANNEL's count of what it actually put on a socket,
    # not the harness's count of what it handed over. Those differ whenever the
    # app is unreachable — the queue accepts every record and delivers none —
    # and reporting the offer count as "observed" would be a metric claiming
    # success over something that never happened, which is the failure this
    # platform's rules exist to prevent.
    delivered = client.sent if client is not None else 0
    log.info(
        "run %s finished: %s (seq=%d rows=%d unflushed=%d offered=%d delivered=%d observed=%s)",
        outcome.run_id,
        outcome.status,
        outcome.seq_issued,
        outcome.rows_written,
        outcome.unflushed,
        outcome.live_offered,
        delivered,
        delivered > 0,
    )
    if outcome.detail:
        log.info("detail: %s", outcome.detail)

    # A cancelled run is a clean outcome, not a failure — it did what was asked.
    return 0 if outcome.status in (RunStatus.SUCCEEDED, RunStatus.CANCELLED) else 1


if __name__ == "__main__":
    sys.exit(main())
