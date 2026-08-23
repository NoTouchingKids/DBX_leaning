"""The ServiceHub: everything long-lived, built once in ``lifespan``.

No module-level globals holding live objects, and no bare accessor that
assumes everything initialised. A service that fails to start is recorded as
degraded and stays ``None``, so a route depending on it can return a clean
503 instead of an AttributeError from somewhere three frames deep.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from shared.envelope import Message, StatusMessage
from shared.protocol import ControlFrame, pack_frame
from shared.tables import TableSet

from .broadcaster import Broadcaster, InProcessBroadcaster
from .config import AppConfig
from .jobs_api import JobsApi
from .repository import RunRepository
from .sql import SqlClient

log = logging.getLogger(__name__)

__all__ = ["ServiceHub", "JobConnections"]


class JobConnections:
    """Live WebSocket connections from jobs, one per run.

    The *only* path by which anything reaches a running job — which is why
    cancel goes through the app and never through a status table a client
    polls.
    """

    def __init__(self) -> None:
        self._by_run: dict[str, Any] = {}

    def register(self, run_id: str, ws: Any) -> None:
        self._by_run[run_id] = ws

    def unregister(self, run_id: str, ws: Any | None = None) -> None:
        if ws is None or self._by_run.get(run_id) is ws:
            self._by_run.pop(run_id, None)

    def is_connected(self, run_id: str) -> bool:
        return run_id in self._by_run

    async def send(self, run_id: str, frame: ControlFrame) -> bool:
        ws = self._by_run.get(run_id)
        if ws is None:
            return False
        try:
            await ws.send_bytes(pack_frame(frame))
        except Exception:  # noqa: BLE001
            log.info("job ws for %s failed on send; dropping it", run_id)
            self.unregister(run_id, ws)
            return False
        return True

    @property
    def run_ids(self) -> list[str]:
        return list(self._by_run)


class ServiceHub:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.tables = TableSet(catalog=config.catalog, schema=config.schema)
        self.broadcaster: Broadcaster = InProcessBroadcaster(queue_max=config.sse_queue_max)
        #: Live WebSockets from jobs. Named apart from `jobs_api` on purpose —
        #: one is a socket registry, the other is the Databricks REST client.
        self.job_sockets = JobConnections()
        self.jobs_api: JobsApi | None = None
        self.sql: SqlClient | None = None
        self.repo: RunRepository | None = None
        self.degraded: dict[str, str] = {}
        self.messages_ingested = 0
        self.status_writes = 0
        self._status_tasks: set[asyncio.Task] = set()

    async def startup(self) -> None:
        cfg = self.config
        sql = SqlClient(
            cfg.workspace_host,
            cfg.warehouse_id,
            cfg.token,
            wait_timeout_s=cfg.sql_wait_timeout_s,
            timeout_s=cfg.sql_timeout_s,
        )
        if sql.available:
            self.sql = sql
            self.repo = RunRepository(sql, self.tables)
        else:
            # Live streaming still works without a warehouse; only backfill,
            # status reads and reconciliation degrade.
            self.degraded["sql"] = (
                "no SQL warehouse configured (DATABRICKS_HOST / DBX_WAREHOUSE_ID); "
                "backfill and reconciliation are unavailable"
            )
            log.warning(self.degraded["sql"])

        jobs_api = JobsApi(cfg.workspace_host, cfg.token)
        if jobs_api.available:
            self.jobs_api = jobs_api
        else:
            self.degraded["jobs_api"] = (
                "no workspace host configured (DATABRICKS_HOST); runs cannot be triggered "
                "from here, though jobs triggered elsewhere are still observed"
            )
            log.warning(self.degraded["jobs_api"])

        if not cfg.job_ids and cfg.default_job_id is None:
            self.degraded["job_ids"] = (
                "no DBX_JOB_IDS configured; no model can be triggered from this app"
            )

    async def shutdown(self) -> None:
        for task in tuple(self._status_tasks):
            task.cancel()
        if self._status_tasks:
            await asyncio.gather(*self._status_tasks, return_exceptions=True)
        if self.sql is not None:
            await self.sql.close()
        if self.jobs_api is not None:
            await self.jobs_api.close()

    async def ingest(self, run_id: str, msg: Message) -> None:
        """One entry point for everything arriving from a job, whichever
        channel it came in on. WS and HTTP push must not diverge."""
        self.messages_ingested += 1
        await self.broadcaster.publish(run_id, msg)
        if isinstance(msg, StatusMessage):
            self._persist_status(run_id, msg)

    def _persist_status(self, run_id: str, msg: StatusMessage) -> None:
        """Reflect a lifecycle transition into ``run_status``.

        The status *message* is a notification; the ``run_status`` row is the
        record of truth (docs/message-envelope-spec.md). This is what keeps
        the two in step while the app is up — when it is not, the job's own
        ``run_events`` carries the truth and startup reconciliation catches up.

        Off the ingest path deliberately: a cold warehouse can take seconds to
        answer, and blocking the job's socket on that would make the app the
        thing a run depends on. It is a couple of statements per run
        (RUNNING, then terminal), not a loop.
        """
        if self.repo is None:
            return

        async def write() -> None:
            try:
                await self.repo.set_run_status(run_id, msg.status.value, detail=msg.detail)
                self.status_writes += 1
            except Exception:  # noqa: BLE001 - the durable record still stands
                log.warning(
                    "could not update run_status for %s -> %s; run_events has it and "
                    "startup reconciliation will pick it up",
                    run_id,
                    msg.status.value,
                    exc_info=True,
                )

        task = asyncio.create_task(write(), name=f"run-status-{run_id}")
        self._status_tasks.add(task)
        task.add_done_callback(self._status_tasks.discard)
