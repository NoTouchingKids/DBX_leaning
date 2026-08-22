"""The ServiceHub: everything long-lived, built once in ``lifespan``.

No module-level globals holding live objects, and no bare accessor that
assumes everything initialised. A service that fails to start is recorded as
degraded and stays ``None``, so a route depending on it can return a clean
503 instead of an AttributeError from somewhere three frames deep.
"""

from __future__ import annotations

import logging
from typing import Any

from shared.envelope import Message
from shared.protocol import ControlFrame, pack_frame
from shared.tables import TableSet

from .broadcaster import Broadcaster, InProcessBroadcaster
from .config import AppConfig
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
        self.jobs = JobConnections()
        self.sql: SqlClient | None = None
        self.repo: RunRepository | None = None
        self.degraded: dict[str, str] = {}
        self.messages_ingested = 0

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

    async def shutdown(self) -> None:
        if self.sql is not None:
            await self.sql.close()

    async def ingest(self, run_id: str, msg: Message) -> None:
        """One entry point for everything arriving from a job, whichever
        channel it came in on. WS and HTTP push must not diverge."""
        self.messages_ingested += 1
        await self.broadcaster.publish(run_id, msg)
