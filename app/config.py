"""App configuration from the environment.

A Databricks App gets its identity from the platform (OAuth M2M via
DATABRICKS_CLIENT_ID/SECRET, or a PAT for local development). Nothing here
takes a user token: this build has no on-behalf-of-user path at all.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field

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

    #: Which Databricks job runs which model: {"scenario": 1234, ...}. This
    #: map is also the allow-list — a model with no job here cannot be
    #: triggered, so the app never needs to import models/ (and its gurobipy /
    #: sklearn / emcee weight) just to validate a request.
    job_ids: dict[str, int] = field(default_factory=dict)
    #: Fallback for a single generic harness job parameterised by model.
    default_job_id: int | None = None

    #: This app's own externally reachable URL, handed to the job so it knows
    #: where to attach. Absent = jobs run unobserved, which is a normal case.
    public_url: str | None = None

    #: Free Edition allows **5 concurrent job tasks per account**, across all
    #: models combined. The trigger endpoint refuses past this rather than
    #: letting Databricks queue or reject the run opaquely.
    max_concurrent_runs: int = 5

    #: Lakebase (managed Postgres) for run_status — the one OLTP-shaped
    #: thing here. Absent means fall back to the warehouse-backed store, so a
    #: deployment is never blocked on provisioning a database.
    lakebase_dsn: str | None = None

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
        raw_jobs = (e.get("DBX_JOB_IDS") or "").strip()
        try:
            job_ids = {str(k): int(v) for k, v in json.loads(raw_jobs).items()} if raw_jobs else {}
        except (json.JSONDecodeError, AttributeError, TypeError, ValueError) as exc:
            raise ValueError(
                f'DBX_JOB_IDS must be a JSON object of model -> job id, e.g. '
                f'{{"scenario": 1234}}; got {raw_jobs!r} ({exc})'
            ) from None

        public_url = (e.get("DBX_APP_PUBLIC_URL") or "").strip().rstrip("/") or None
        lakebase_dsn = _lakebase_dsn(e)
        default_job = (e.get("DBX_JOB_ID") or "").strip()

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
            job_ids=job_ids,
            default_job_id=int(default_job) if default_job else None,
            public_url=public_url,
            lakebase_dsn=lakebase_dsn,
            max_concurrent_runs=int(e.get("DBX_MAX_CONCURRENT_RUNS", "5")),
            job_token=(e.get("DBX_APP_TOKEN") or "").strip() or None,
            reconcile_on_startup=_flag("DBX_RECONCILE_ON_STARTUP", True),
        )

    def job_id_for(self, model: str) -> int | None:
        """Which job runs this model, or None if it cannot be triggered."""
        return self.job_ids.get(model, self.default_job_id)

    @property
    def triggerable_models(self) -> list[str]:
        return sorted(self.job_ids)


def _lakebase_dsn(e: Mapping[str, str]) -> str | None:
    """Build the Lakebase connection string, if one is configured.

    Either a whole DSN, or the parts. The password is a short-lived
    Databricks OAuth token, which is why ``app/store.py`` opens a connection
    per operation rather than pooling — resolving the credential at connect
    time makes rotation a non-issue.
    """
    dsn = (e.get("DBX_LAKEBASE_DSN") or "").strip()
    if dsn:
        return dsn

    host = (e.get("DBX_LAKEBASE_HOST") or "").strip()
    if not host:
        return None

    database = (e.get("DBX_LAKEBASE_DATABASE") or "databricks_postgres").strip()
    user = (e.get("DBX_LAKEBASE_USER") or "").strip()
    password = (e.get("DBX_LAKEBASE_PASSWORD") or e.get("DATABRICKS_TOKEN") or "").strip()
    port = (e.get("DBX_LAKEBASE_PORT") or "5432").strip()

    credentials = user
    if password:
        credentials = f"{user}:{password}"
    prefix = f"{credentials}@" if credentials else ""
    # Lakebase requires TLS.
    return f"postgresql://{prefix}{host}:{port}/{database}?sslmode=require"
