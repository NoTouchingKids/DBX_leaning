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

**SIGINT is handled differently, and deliberately not the same way.**
`ipykernel` — which is what actually executes a serverless
`spark_python_task`, see above — installs its own SIGINT handler on
startup; that's the entire mechanism behind "Interrupt Kernel" raising
`KeyboardInterrupt` in whatever cell is running. Databricks documents
SIGTERM, never SIGINT, as its cancellation signal, so installing our own
SIGINT handler under a real kernel buys this harness nothing and risks
silently replacing a handler the kernel relies on for something else
entirely. So SIGINT is only wired up when nothing already claims to be a
kernel (`run_local`, a plain Ctrl-C). Both handlers, when installed, chain
to whatever was there before — free insurance if "SIGTERM only" ever turns
out to be wrong, and harmless otherwise.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
from typing import Any

from shared.envelope import RunStatus

from .config import JobConfig
from .harness import Harness
from .telemetry import PartFileWriter
from .ws import RpcClient, app_client

log = logging.getLogger("job")


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


def _cancel_and_chain(harness: Harness, old: Any) -> Any:
    """Cancel first, then fall through to whatever handler was already
    installed for this signal — so taking SIGTERM/SIGINT here never
    silently erases someone else's handler (`ipykernel`'s own SIGINT
    handling, in particular). `old` is whatever `signal.getsignal()`
    returned before we installed ours: a real handler, `SIG_DFL`, or
    `SIG_IGN` — only the first of those is callable.
    """

    def handler(s: int, f: Any) -> None:
        harness.token.cancel(f"received {signal.Signals(s).name}")
        if callable(old):
            old(s, f)

    return handler


def _build_client(cfg: JobConfig, harness: Harness) -> RpcClient | None:
    """A live channel, if there is an app to talk to.

    No app is the normal case, not a degraded one — apps run ~8h/day and jobs
    do not. Returning None here is how "nobody is watching" is represented,
    and the run is identical either way apart from the commentary.
    """
    if not cfg.app_url or cfg.ws_url is None:
        log.info("no DBX_APP_URL — running unobserved, durable path only")
        return None

    # BOTH halves come out of the secret scope, read once per run rather than
    # per connection attempt — see `read_client_credentials`. The TOKEN they
    # buy is a separate concern: `app_client` builds one `M2MTokenProvider`
    # for the whole run and it refreshes itself on whichever attempt needs it.
    from .auth import read_client_credentials

    client_id, client_secret = read_client_credentials(
        cfg.oauth_secret_scope, cfg.oauth_client_id_key, cfg.oauth_secret_key
    )

    return app_client(
        cfg.app_url,
        cfg.run_id,
        on_cancel=lambda who: harness.cancel(who),
        on_replay=lambda a, b: harness.replay(a, b),
        next_seq=lambda: harness.seq.issued,
        workspace_host=cfg.workspace_host,
        client_id=client_id,
        client_secret=client_secret,
    )


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    cfg = JobConfig.from_env()

    # THE RUN ID IS ONE VALUE, and this is what makes that true for everything
    # else in the process.
    #
    # `JobConfig` generates `run-<hex>` when DBX_RUN_ID is absent or empty —
    # which is the COMMON case, not an edge one: the job YAML defaults that
    # parameter to `""`, and `run_model.py` drops empty arguments rather than
    # exporting them, so `databricks bundle run` produces exactly this. Without
    # the line below the generated id existed only on `cfg`, and a model
    # reading DBX_RUN_ID for itself saw nothing.
    #
    # A model needs it because a model writes its own results table now, and
    # those rows have to be keyed by the same run the telemetry is. Without
    # this the two disagree — telemetry under `run-a1b2c3`, result rows under
    # whatever the model fell back to — and nothing joins them back together.
    # Neither side errors; the run just becomes two unrelated halves.
    #
    # Assigned rather than `setdefault`: `cfg.run_id` is the authoritative id
    # whichever way it was arrived at, so the environment should agree with it
    # rather than win over it.
    os.environ["DBX_RUN_ID"] = cfg.run_id

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
        harness.channel = client.send
        client.start()

    # Databricks cancels a task with SIGTERM. Treating it as a cancel rather
    # than a kill is what lets the run flush its telemetry and record an honest
    # terminal status. Unlike v3 this cannot fail for being off the main
    # thread, because `main()` IS the main thread now.
    #
    # SIGINT is skipped under a real kernel — see the module docstring for
    # why — and installed only when nothing already claims to be one, e.g.
    # `run_local()`'s plain Ctrl-C case.
    sigs = (signal.SIGTERM,) if "ipykernel" in sys.modules else (signal.SIGTERM, signal.SIGINT)
    for sig in sigs:
        try:
            signal.signal(sig, _cancel_and_chain(harness, signal.getsignal(sig)))
        except (ValueError, OSError) as exc:
            # Only if something else already owns the handler in a way that
            # rejects ours outright. Cancel over the RPC channel is
            # unaffected; what is lost is the platform's own task
            # cancellation being graceful.
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
