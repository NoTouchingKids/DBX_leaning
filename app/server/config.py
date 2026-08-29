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

from .store import DEFAULT_SCHEMA as DEFAULT_LAKEBASE_SCHEMA

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
    #: Total time one statement may take — the API's own wait plus the client's
    #: polling. Generous on purpose: what it is usually waiting for is a
    #: warehouse cold start, and auto-stop is 10 minutes, so the first read of
    #: the day nearly always pays it. Failing at 30s instead is what produced
    #: "statement CANCELED: no detail" across every route at once.
    sql_statement_deadline_s: float = 180.0

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
    #: triggered, so the app never needs to import job/models/ (and its gurobipy /
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

    #: The Postgres schema `run_status` lives in. Deliberately not `public`:
    #: since PostgreSQL 15 that schema no longer grants CREATE to `PUBLIC`, so
    #: a role that does not own the database cannot create a table there. The
    #: default is imported from `store.py` rather than retyped, so the two
    #: cannot drift.
    lakebase_schema: str = DEFAULT_LAKEBASE_SCHEMA

    #: Shared secret a job presents on the WS/push ingress. Distinct from any
    #: user auth: the platform proxy authenticates humans, this authenticates
    #: the job process.
    job_token: str | None = None

    reconcile_on_startup: bool = True

    #: How often the orphan sweep looks for a run whose job died without
    #: telling anyone (OOM, SIGKILL, a lost cluster — no teardown runs, so no
    #: terminal status lands anywhere, including Lakebase). Unlike startup
    #: reconciliation, this repeats for as long as the app is up, because a
    #: crashed run otherwise holds one of Free Edition's 5 account-wide task
    #: slots until the *next* restart — hours away, for an app that runs
    #: ~8h/day. A couple of minutes is frequent enough that a stuck slot does
    #: not matter much, and infrequent enough that the Postgres read and the
    #: handful of Jobs API calls it costs are noise.
    orphan_sweep_interval_s: float = 120.0

    #: How old a run must be (since `claim_slot` wrote its row) before a
    #: missing socket counts as suspicious rather than ordinary. The app
    #: registers the run *before* `run-now` returns, and a serverless job task
    #: then takes tens of seconds to start and dial back in — so a run that is
    #: seconds old with nothing attached yet is the normal shape of "just
    #: launched", not a corpse. Comfortably longer than that cold start.
    orphan_sweep_min_age_s: float = 180.0

    #: How long a run must have gone with no status update before its missing
    #: socket is trusted rather than read as an ordinary reconnect in
    #: progress. The job redials on a timer of its own — `DBX_WS_RECONNECT_S`
    #: in `job/config.py`, 30s by default — so this has to comfortably clear
    #: that (with room for the reconnect itself, plus a `hello`, to complete)
    #: or a single missed redial would read as a death. 5x that default.
    orphan_sweep_socket_grace_s: float = 150.0

    #: Where the built React bundle lives. Databricks Apps has no Node
    #: runtime, so this app serves the SPA itself (see app/server/spa.py). Relative
    #: paths are resolved against the repo root, not the process cwd. Absent
    #: on a source checkout and during tests — that degrades, it is not fatal.
    #:
    #: `dist/`, resolved against the APP root — `app/`, the folder
    #: `resources/app.yml` hands Databricks Apps. `app/client/` is the client
    #: source and never deploys; `app/client/vite.config.ts` writes `../dist`,
    #: so the built output lands beside `server/` and always deploys.
    frontend_dist: str = "dist"

    #: The app's durable filesystem — a Unity Catalog volume, mounted at
    #: ``/Volumes/<catalog>/<schema>/<name>``. Empty means none is configured.
    #:
    #: Not the app's own disk, which is not storage: a Databricks App runs at
    #: most 24 hours and then stops, taking its container with it, and a
    #: redeploy does the same. A file written beside the code is downloadable
    #: until the next restart, which is worse than not offering it at all.
    #:
    #: Nothing on the run path touches this — telemetry goes to Delta and
    #: results to their own tables — so an absent volume degrades rather than
    #: fails, and `/healthz` says so.
    app_volume: str | None = None

    #: The app's own service principal, injected by Databricks Apps. Exchanged
    #: for a short-lived OAuth token at `/oidc/v1/token` (see `oauth.py`),
    #: which is the ONLY credential a Lakebase instance accepts unless
    #: `enable_pg_native_login` was turned on — and it is off by default.
    #:
    #: Absent, the run store falls back to a static password from
    #: `DBX_LAKEBASE_PASSWORD` or `DATABRICKS_TOKEN`, which is what the local
    #: dev stack and a native-login instance need.
    oauth_client_id: str | None = None
    oauth_client_secret: str | None = None

    #: The Postgres role, kept alongside the DSN it is already baked into.
    #:
    #: Duplicated on purpose. `services.py` has to compare it against
    #: `oauth_client_id`: Lakebase's role is named after the principal whose
    #: OAuth token is presented, so connecting as one while presenting the
    #: other's token fails as an ordinary authentication error — which reads
    #: as a wrong secret and sends whoever is debugging it to the secret scope.
    #: Parsing it back out of the DSN to make that comparison would be worse.
    lakebase_user: str | None = None

    @property
    def has_client_credentials(self) -> bool:
        return bool(self.oauth_client_id and self.oauth_client_secret and self.workspace_host)

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
                f"DBX_JOB_IDS must be a JSON object of model -> job id, e.g. "
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
            sql_statement_deadline_s=float(e.get("DBX_SQL_STATEMENT_DEADLINE_S", "180")),
            sse_keepalive_s=float(e.get("DBX_SSE_KEEPALIVE_S", "10")),
            sse_queue_max=int(e.get("DBX_SSE_QUEUE_MAX", "1000")),
            backfill_page_size=int(e.get("DBX_BACKFILL_PAGE_SIZE", "5000")),
            job_ids=job_ids,
            default_job_id=int(default_job) if default_job else None,
            public_url=public_url,
            lakebase_dsn=lakebase_dsn,
            lakebase_schema=(e.get("DBX_LAKEBASE_SCHEMA") or "").strip() or DEFAULT_LAKEBASE_SCHEMA,
            max_concurrent_runs=int(e.get("DBX_MAX_CONCURRENT_RUNS", "5")),
            job_token=(e.get("DBX_APP_TOKEN") or "").strip() or None,
            reconcile_on_startup=_flag("DBX_RECONCILE_ON_STARTUP", True),
            orphan_sweep_interval_s=float(e.get("DBX_ORPHAN_SWEEP_INTERVAL_S", "120")),
            orphan_sweep_min_age_s=float(e.get("DBX_ORPHAN_SWEEP_MIN_AGE_S", "180")),
            orphan_sweep_socket_grace_s=float(e.get("DBX_ORPHAN_SWEEP_SOCKET_GRACE_S", "150")),
            frontend_dist=(e.get("DBX_FRONTEND_DIST") or "").strip() or "dist",
            app_volume=(e.get("DBX_APP_VOLUME") or "").strip() or None,
            lakebase_user=(e.get("DBX_LAKEBASE_USER") or "").strip() or None,
            oauth_client_id=(
                e.get("DBX_OAUTH_CLIENT_ID") or e.get("DATABRICKS_CLIENT_ID") or ""
            ).strip()
            or None,
            oauth_client_secret=(
                e.get("DBX_OAUTH_CLIENT_SECRET") or e.get("DATABRICKS_CLIENT_SECRET") or ""
            ).strip()
            or None,
        )

    def job_id_for(self, model: str) -> int | None:
        """Which job runs this model, or None if it cannot be triggered."""
        return self.job_ids.get(model, self.default_job_id)

    @property
    def triggerable_models(self) -> list[str]:
        return sorted(self.job_ids)


def _lakebase_dsn(e: Mapping[str, str]) -> str | None:
    """Build the Lakebase connection string, if one is configured.

    Either a whole DSN, or the parts.

    **A password here is the fallback, not the main path.** Lakebase accepts a
    short-lived OAuth token and, unless ``enable_pg_native_login`` was turned
    on at creation, nothing else — so for a real deployment this returns a DSN
    with NO credential in it and ``services.py`` hands the store an
    :class:`~server.oauth.OAuthTokenProvider` that resolves one per
    connection. A token baked in here would be valid for about an hour against
    an app that runs for up to 24.

    ``DBX_LAKEBASE_PASSWORD`` and ``DATABRICKS_TOKEN`` are still read, for the
    local dev stack (embedded Postgres, no auth) and for an instance with
    native login enabled.
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
