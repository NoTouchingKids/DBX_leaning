"""The ServiceHub: everything long-lived, built once in ``lifespan``.

No module-level globals holding live objects, and no bare accessor that
assumes everything initialised. A service that fails to start is recorded as
degraded and stays ``None``, so a route depending on it can return a clean
503 instead of an AttributeError from somewhere three frames deep.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

from shared.envelope import Message, StatusMessage
from shared.protocol import ControlFrame, pack_frame
from shared.tables import TableSet

from .broadcaster import Broadcaster, InProcessBroadcaster
from .config import AppConfig
from .discovery import map_jobs_to_models
from .jobs_api import JobsApi
from .oauth import OAuthTokenProvider
from .repository import RunRepository
from .sql import SqlClient
from .store import PostgresRunStore, RunStore, WarehouseRunStore

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
        #: Run state. Postgres when Lakebase is configured, else the
        #: warehouse-backed one — see app/server/store.py for why it moved.
        self.store: RunStore | None = None
        self.degraded: dict[str, str] = {}
        #: Where `config.job_ids` came from — "config" when DBX_JOB_IDS was
        #: set, "discovered" when the workspace was asked instead, "none" when
        #: neither worked. Reported on `/healthz` and `/api/models`, because
        #: "discovered" means the live app deployment was not created by the
        #: bundle, and so nothing else in `resources/app.yml` reached the app
        #: either — the app volume, the ingress token, the Lakebase host.
        self.job_ids_source: str = "none"
        #: The app's durable filesystem, or None when unconfigured or
        #: unreachable. A route needing it should 503 rather than fall back
        #: to local disk, which disappears with the container.
        self.volume: Path | None = None
        #: One OAuth token for Postgres, Unity Catalog and the Jobs API — the
        #: whole app authenticates as one service principal. None when there
        #: are no client credentials, which leaves each client on whatever
        #: static token it was given.
        self.token_provider = None
        self.messages_ingested = 0
        self.status_writes = 0
        self._status_tasks: set[asyncio.Task] = set()

    async def startup(self) -> None:
        cfg = self.config
        self.token_provider = self._token_source(cfg)
        sql = SqlClient(
            cfg.workspace_host,
            cfg.warehouse_id,
            cfg.token,
            token_provider=self.token_provider,
            wait_timeout_s=cfg.sql_wait_timeout_s,
            timeout_s=cfg.sql_timeout_s,
            statement_deadline_s=cfg.sql_statement_deadline_s,
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

        await self._start_store(cfg)

        jobs_api = JobsApi(cfg.workspace_host, cfg.token, token_provider=self.token_provider)
        if jobs_api.available:
            self.jobs_api = jobs_api
        else:
            self.degraded["jobs_api"] = (
                "no workspace host configured (DATABRICKS_HOST); runs cannot be triggered "
                "from here, though jobs triggered elsewhere are still observed"
            )
            log.warning(self.degraded["jobs_api"])

        await self._resolve_job_ids(cfg)
        self._check_volume(cfg)

    async def _resolve_job_ids(self, cfg: AppConfig) -> None:
        """Make sure the app knows which job runs which model.

        `DBX_JOB_IDS` is the normal answer and always wins — it is the
        allow-list as well as the map, so a deployment that names three models
        means three, not "and whatever else is in the workspace".

        Absent, ask the workspace. The env var only reaches the app when the
        LIVE app deployment was created by the bundle; a deploy that skipped
        `bundle run`, or a redeploy from the Apps UI, leaves the app running an
        environment built from `app/app.yaml`, which cannot have job ids
        because a hand deploy has no bundle to interpolate them from. That
        produced an app where everything worked except that `/api/models` was
        empty, with nothing on the app itself to point at.

        Discovery is best-effort by construction: it needs the Jobs API, which
        needs a host and a credential, and any of those missing lands in the
        same degraded state as before. It never raises into startup.
        """
        if cfg.job_ids or cfg.default_job_id is not None:
            self.job_ids_source = "config"
            return

        if self.jobs_api is None:
            self.degraded["job_ids"] = (
                "no DBX_JOB_IDS configured and no Jobs API to discover them from; "
                "no model can be triggered from this app"
            )
            log.warning(self.degraded["job_ids"])
            return

        try:
            # Bounded: startup must not hang on a slow or wedged workspace.
            jobs = await asyncio.wait_for(self.jobs_api.list_jobs(), timeout=30)
        except Exception as exc:  # noqa: BLE001 - a failed lookup is degraded, not fatal
            self.degraded["job_ids"] = (
                f"no DBX_JOB_IDS configured and discovering jobs failed ({exc}); "
                "no model can be triggered from this app"
            )
            log.warning(self.degraded["job_ids"], exc_info=True)
            return

        found = map_jobs_to_models(jobs)
        if not found.job_ids:
            self.degraded["job_ids"] = (
                f"no DBX_JOB_IDS configured, and none of the {len(jobs)} jobs visible to "
                "this app are tagged project=dbx-leaning or named '... dbx-leaning · <model>'; "
                "no model can be triggered from this app"
            )
            log.warning(self.degraded["job_ids"])
            return

        self.config = replace(cfg, job_ids=found.job_ids)
        self.job_ids_source = "discovered"
        log.warning(
            "DBX_JOB_IDS was not set; discovered %d job(s) from the workspace: %s. "
            "This works, but it means the live app deployment was not created by "
            "`databricks bundle run`, so nothing else in resources/app.yml reached "
            "the app either.",
            len(found.job_ids),
            ", ".join(found.job_ids),
        )
        if found.ambiguous:
            self.degraded["job_ids_ambiguous"] = (
                "more than one job claims the same model, so the highest id won: "
                + "; ".join(f"{m}: {ids}" for m, ids in found.ambiguous.items())
            )
            log.warning(self.degraded["job_ids_ambiguous"])

    def _check_volume(self, cfg: AppConfig) -> None:
        """Is the app's durable filesystem actually there?

        Checked at startup rather than at first write, because the failure is
        a grant that was never applied or a volume that was never created —
        both fixed by a human, both invisible until someone tries to download
        a file. Nothing on the run path depends on it, so it degrades.
        """
        if not cfg.app_volume:
            self.degraded["volume"] = (
                "no DBX_APP_VOLUME configured; the app has no durable place to "
                "put a file, and anything that would write one is unavailable"
            )
            return

        path = Path(cfg.app_volume)
        if not path.is_dir():
            self.degraded["volume"] = (
                f"DBX_APP_VOLUME is {cfg.app_volume}, which is not a directory here. "
                "Apply uc_ddl/003_app_volume.sql, and check the app has "
                "READ_VOLUME and WRITE_VOLUME on it (resources/app.yml)"
            )
            log.warning(self.degraded["volume"])
            return

        self.volume = path

    def _token_source(self, cfg: AppConfig):
        """One OAuth token, awaited by all three things that need a credential.

        A deployment authenticates as a single service principal against
        Postgres, Unity Catalog and the Jobs API, so there is one exchange and
        one cache — `oauth.py` holds the token until shortly before it
        expires. Built once here rather than per client so those three do not
        each keep their own copy on their own refresh schedule.

        Returns None when there are no client credentials, which is the local
        dev stack and any deployment still using a static `DATABRICKS_TOKEN`.
        """
        if not cfg.has_client_credentials:
            if cfg.oauth_client_id and not cfg.oauth_client_secret:
                # Half-configured, and the half that is missing is the one a
                # deploy can silently lose: the secret is a `value_from` in
                # `resources/app.yml` that is commented out by default,
                # because a declared secret resource is validated at deploy
                # time and 404s the whole deploy if the key is absent. Someone
                # who set the id meant to run as that principal; falling back
                # to the app's own without saying so would look like it worked.
                self.degraded["oauth"] = (
                    f"DBX_OAUTH_CLIENT_ID is set ({cfg.oauth_client_id}) but no "
                    "DBX_OAUTH_CLIENT_SECRET; running as the app's own service "
                    "principal instead. Uncomment the oauth-client-secret block "
                    "in resources/app.yml, and create the secret first"
                )
                log.warning(self.degraded["oauth"])
            return None

        provider = OAuthTokenProvider(
            cfg.workspace_host,  # type: ignore[arg-type]  # has_client_credentials checked it
            cfg.oauth_client_id,  # type: ignore[arg-type]
            cfg.oauth_client_secret,  # type: ignore[arg-type]
        )
        log.info("credential: OAuth token for %s from %s", cfg.oauth_client_id, provider.url)
        return provider.token

    def _check_lakebase_identity(self, cfg: AppConfig) -> None:
        """Is the app connecting as the principal whose token it presents?

        Lakebase takes an OAuth token as its password and the Postgres role is
        named after the principal that token belongs to. Presenting one
        principal's token while connecting as another's role fails as an
        ordinary authentication error — which reads as a wrong secret, and
        sends whoever is debugging it into the secret scope rather than here.

        Checked at startup because the answer cannot change afterwards, and
        because the alternative is finding out on the first trigger.
        """
        if not cfg.has_client_credentials or not cfg.lakebase_user:
            return
        if cfg.lakebase_user == cfg.oauth_client_id:
            return
        self.degraded["lakebase_identity"] = (
            f"connecting to Lakebase as {cfg.lakebase_user!r} while presenting a token "
            f"for {cfg.oauth_client_id!r}. The Postgres role is named after the "
            "principal the token belongs to, so this fails as an authentication "
            "error that looks like a bad secret. Set DBX_LAKEBASE_USER to the same "
            "application id."
        )
        log.error(self.degraded["lakebase_identity"])

    async def _start_store(self, cfg: AppConfig) -> None:
        """Pick the run store once, and say which one loudly.

        A deployment that thinks it is on Lakebase while silently running on
        the warehouse would keep the concurrency race and the missing primary
        key without anyone noticing.
        """
        if cfg.lakebase_dsn:
            self._check_lakebase_identity(cfg)
            store: RunStore = PostgresRunStore(
                cfg.lakebase_dsn,
                schema=cfg.lakebase_schema,
                password_provider=self.token_provider,
            )
            try:
                await store.ensure_schema()
            except Exception as exc:  # noqa: BLE001
                self.degraded["lakebase"] = f"Lakebase configured but unreachable: {exc}"
                log.error(self.degraded["lakebase"])
            else:
                self.store = store
                version = getattr(store, "server_version", None)
                log.info("run store: Lakebase (postgres %s)", version or "version unknown")
                return

        if self.repo is not None:
            self.store = WarehouseRunStore(self.repo)
            log.info(
                "run store: SQL warehouse. No Lakebase configured, so the "
                "concurrency ceiling is checked without a transaction and a "
                "duplicate run_id is not refused — see app/server/store.py."
            )
            return

        self.degraded["store"] = (
            "no run store: neither Lakebase nor a SQL warehouse is configured; "
            "runs cannot be registered, listed or triggered"
        )
        log.warning(self.degraded["store"])

    async def shutdown(self) -> None:
        for task in tuple(self._status_tasks):
            task.cancel()
        if self._status_tasks:
            await asyncio.gather(*self._status_tasks, return_exceptions=True)
        if self.sql is not None:
            await self.sql.close()
        if self.jobs_api is not None:
            await self.jobs_api.close()
        if self.store is not None:
            await self.store.close()

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
        store = self.store
        if store is None:
            return

        async def write() -> None:
            # Bound above, not re-read here: the None-check happens now, the
            # await happens later, and the attribute could have changed.
            try:
                await store.set_status(run_id, msg.status, detail=msg.detail)
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
