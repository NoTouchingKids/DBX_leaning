"""App configuration from the environment.

A Databricks App gets its identity from the platform (OAuth M2M via
DATABRICKS_CLIENT_ID/SECRET, or a PAT for local development). Nothing here
takes a user token: this build has no on-behalf-of-user path at all.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

__all__ = ["AppConfig"]


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    return default if raw is None else raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class AppConfig:
    catalog: str = "main"
    schema: str = "dbx_leaning"

    #: Workspace host, e.g. https://dbc-xxxx.cloud.databricks.com
    workspace_host: str | None = None
    warehouse_id: str | None = None
    token: str | None = None

    #: Statement Execution API's own wait, so a fast query is one round trip
    #: rather than a poll loop. 5-50s per the API.
    sql_wait_timeout_s: int = 30
    sql_timeout_s: float = 60.0

    #: SSE keepalive. Comment-only lines, frequent enough to tell an idle
    #: timeout from a duration cap if the ingress cuts us (see /spike-sse).
    sse_keepalive_s: float = 10.0
    #: Bounded per-subscriber; a browser that stops reading must not be able
    #: to grow the app's memory without limit.
    sse_queue_max: int = 1000
    #: Rows returned per backfill page. INLINE + JSON_ARRAY aborts past
    #: 25 MiB, so this stays well clear of it.
    backfill_page_size: int = 5000

    #: Shared secret a job presents on the WS/push ingress. Distinct from any
    #: user auth: the platform proxy authenticates humans, this authenticates
    #: the job process.
    job_token: str | None = None

    reconcile_on_startup: bool = True

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> AppConfig:
        e = os.environ if env is None else env
        host = (e.get("DATABRICKS_HOST") or "").strip().rstrip("/") or None
        if host and not host.startswith("http"):
            host = f"https://{host}"
        return cls(
            catalog=e.get("DBX_CATALOG", "main"),
            schema=e.get("DBX_SCHEMA", "dbx_leaning"),
            workspace_host=host,
            warehouse_id=(e.get("DBX_WAREHOUSE_ID") or "").strip() or None,
            token=(e.get("DATABRICKS_TOKEN") or "").strip() or None,
            sql_wait_timeout_s=int(e.get("DBX_SQL_WAIT_TIMEOUT_S", "30")),
            sql_timeout_s=float(e.get("DBX_SQL_TIMEOUT_S", "60")),
            sse_keepalive_s=float(e.get("DBX_SSE_KEEPALIVE_S", "10")),
            sse_queue_max=int(e.get("DBX_SSE_QUEUE_MAX", "1000")),
            backfill_page_size=int(e.get("DBX_BACKFILL_PAGE_SIZE", "5000")),
            job_token=(e.get("DBX_APP_TOKEN") or "").strip() or None,
            reconcile_on_startup=_flag("DBX_RECONCILE_ON_STARTUP", True),
        )
