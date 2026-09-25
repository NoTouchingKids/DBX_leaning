"""The ServiceHub: everything long-lived, built once in ``lifespan``.

No module-level globals holding live objects, and no bare accessor that
assumes everything initialised. A service that fails to start is recorded as
degraded and stays ``None``, so a route depending on it can return a clean
503 instead of an AttributeError from somewhere three frames deep.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from shared.envelope import Message
from shared.rpc import Response, RpcError, request
from shared.tables import TableSet

from .broadcaster import Broadcaster, InProcessBroadcaster
from .config import AppConfig
from .discovery import DiscoveryStatus, map_jobs_to_models
from .jobs_api import JobsApi
from .oauth import OAuthTokenProvider
from .store import PostgresRunStore

log = logging.getLogger(__name__)

__all__ = ["ServiceHub", "JobConnections"]


class JobConnections:
    """Live RPC connections from jobs, one per run.

    The *only* path by which anything reaches a running job — which is why
    cancel goes through the app and never through a status table a client
    polls, and why `replay` is possible at all now that the app holds no grant
    on the telemetry volume.

    It also holds the outstanding calls. A request the APP makes is answered on
    the same socket, interleaved with whatever telemetry the job is streaming,
    so `call()` parks a future under the request id and `resolve()` — driven by
    the receive loop in `routes/rpc.py` — completes it. That indirection is the
    price of request/response on a shared duplex channel, and it is the whole
    of it.
    """

    def __init__(self) -> None:
        self._by_run: dict[str, Any] = {}
        self._pending: dict[tuple[str, int], asyncio.Future] = {}
        self._next_id = 0

    def register(self, run_id: str, ws: Any) -> None:
        self._by_run[run_id] = ws

    def unregister(self, run_id: str, ws: Any | None = None) -> None:
        if ws is not None and self._by_run.get(run_id) is not ws:
            # A STALE socket, and it must not touch anything. A job whose
            # connection is half-open reconnects on its own backoff long
            # before the app's `receive_text` on the dead one raises, so the
            # ordering here is: new socket registers, a cancel is sent on it,
            # and only THEN does this coroutine notice its own socket died.
            # Failing the pending calls at that point answers a cancel the
            # live socket may already have delivered with "nobody heard you"
            # — the exact confusion an acknowledged cancel exists to remove,
            # and one that sends a user to `databricks jobs cancel-run` for a
            # run that is already stopping.
            return

        self._by_run.pop(run_id, None)
        # Fail every call waiting on this run rather than leaving a caller
        # hanging until its own timeout: the socket is gone, and the answer is
        # never arriving.
        for key, fut in list(self._pending.items()):
            if key[0] == run_id and not fut.done():
                fut.set_exception(ConnectionError(f"job for {run_id} disconnected"))
                self._pending.pop(key, None)

    def is_connected(self, run_id: str) -> bool:
        return run_id in self._by_run

    async def call(
        self,
        run_id: str,
        method: str,
        params: dict[str, Any],
        *,
        timeout_s: float = 10.0,
    ):
        """Ask the job something and wait for its answer.

        Raises `ConnectionError` when no job is attached — which is a normal
        state, not an error in itself, and the caller decides what it means.
        """
        ws = self._by_run.get(run_id)
        if ws is None:
            raise ConnectionError(f"no job attached for {run_id}")

        self._next_id += 1
        call_id = self._next_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[(run_id, call_id)] = fut

        try:
            async with asyncio.timeout(timeout_s):
                # The SEND is inside the timeout, and that is not tidiness. A
                # WebSocket send applies backpressure: a job that has stopped
                # reading its socket — which is exactly what a slow `replay`
                # does, since the job answers on the same single thread that
                # reads — fills the send window and `send_text` blocks. With
                # it outside the timeout this coroutine never returns at all,
                # and a cancel that hangs forever is worse than one that
                # fails: the caller is told nothing and never sees the
                # `databricks jobs cancel-run` escape hatch.
                await ws.send_text(request(method, params, id=call_id))
                return await fut
        except TimeoutError:
            raise ConnectionError(
                f"job for {run_id} did not answer {method} in {timeout_s}s"
            ) from None
        except (ConnectionError, RpcError):
            # Already the two shapes the callers know how to render: 409 with
            # the escape hatch, or 502 "the job refused".
            raise
        except Exception as exc:  # noqa: BLE001 - a dead socket is not a bug
            # The socket was registered but is not usable. Starlette raises
            # RuntimeError once a close frame has gone out, and the transport
            # can raise its own errors — none of which any caller catches, so
            # they surfaced as a bare 500. To the user "the job went away as
            # you clicked cancel" is the same event as "no job attached", and
            # only one of those was telling them what to do about it.
            raise ConnectionError(
                f"send to the job for {run_id} failed: {type(exc).__name__}: {exc}"
            ) from exc
        finally:
            self._pending.pop((run_id, call_id), None)

    def resolve(self, run_id: str, response: Response) -> None:
        """Hand a reply to whoever is waiting for it.

        An unmatched id is a late answer to a call that already timed out;
        dropping it is right, and noisier handling would only log during
        exactly the incidents that are already noisy.
        """
        key = (run_id, response.id)
        fut = self._pending.pop(key, None)  # type: ignore[arg-type]
        if fut is None or fut.done():
            return
        if response.ok:
            fut.set_result(response.result)
        else:
            error = response.error or {}
            fut.set_exception(
                RpcError(error.get("code", 0), error.get("message", "job returned an error"))
            )

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
        #: Run state, in Lakebase. None when it is unconfigured, unreachable, or
        #: its schema fails the startup check — see `degraded` for which.
        self.store: PostgresRunStore | None = None
        self.degraded: dict[str, str] = {}
        #: Where `config.job_ids` came from — "config" when DBX_JOB_IDS (or
        #: DBX_JOB_ID) was set, "discovered" when the workspace was asked by
        #: tag, "none" when neither has worked yet. Reported on `/healthz` and
        #: `/api/models`. "discovered" is the normal v4 state, not a warning:
        #: `resources/app.yml` deliberately sets no DBX_JOB_IDS.
        self.job_ids_source: str = "none"
        #: Decided once, from the config the hub was BUILT with — discovery
        #: writes its result into `self.config.job_ids`, so re-reading that
        #: later cannot tell an explicit map from a discovered one.
        self._job_ids_explicit = bool(config.job_ids) or config.default_job_id is not None
        #: How discovery is going, for `/healthz`: tag, interval, last success,
        #: last error. See `refresh_job_ids()`.
        self.discovery = DiscoveryStatus(project_tag=config.project_tag)
        #: The periodic refresh, owned here and cancelled in `shutdown()`.
        self._discovery_task: asyncio.Task | None = None
        #: Serialises the periodic refresh and the on-demand one, so a trigger
        #: arriving mid-refresh waits for that answer instead of asking again.
        self._discovery_lock = asyncio.Lock()
        #: Monotonic time of the last attempt, success or not — what rate-limits
        #: the on-demand refresh in `job_id_for()`.
        self._discovery_attempted_at: float | None = None
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

    async def startup(self) -> None:
        cfg = self.config
        self.token_provider = self._token_source(cfg)

        # No SQL warehouse client here any more, deliberately. v4 takes the
        # warehouse off the app's live path entirely: run state is Postgres,
        # a live gap is replayed by the JOB from its own telemetry log, and a
        # finished run's history arrives in SQL via the ingestion job rather
        # than being queried by the app. See docs/v4-rewrite-plan.md.
        #
        # What went with it: SqlClient, RunRepository, startup reconciliation,
        # and the cold-warehouse start race they all had to handle.

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

        await self.refresh_job_ids()
        self._start_discovery_refresh()
        self._check_volume(cfg)

    async def refresh_job_ids(self) -> bool:
        """Ask the workspace which jobs are ours, by tag. True if the map was replaced.

        **Discovery is the mechanism now, not the fallback.** v3 had it the
        other way round: `DBX_JOB_IDS` was interpolated at deploy time from
        `${resources.jobs.model_x.id}`, and discovery only covered the case
        where that env var did not reach the app.

        That inversion is the point. Registering ids at deploy time means the
        app and the jobs must be built by the same bundle, in the same repo —
        so moving the jobs anywhere else breaks the app, which is exactly the
        coupling v4 exists to remove. A tag does not care where a job is
        defined, who deployed it, or whether it still lives in this
        repository. Out of hundreds of jobs in a workspace, the ones carrying
        `project: <DBX_PROJECT_TAG>` are ours; that is the whole contract.

        `DBX_JOB_IDS` is still honoured when explicitly set, because someone
        who sets it means it — it is an allow-list, narrowing to exactly the
        models named. It is no longer produced by the bundle, and nothing
        depends on it existing. When it is set this does nothing, and no
        periodic refresh is started.

        Called at startup, then every `discovery_refresh_s` by
        `_discovery_loop`, and on demand by `job_id_for()`. **A failed refresh
        keeps the last good map**: a Jobs API blip, or a principal that
        briefly lost access, must not take away every model a working app
        could trigger. It is recorded on `self.discovery` and reported under
        `degraded.job_discovery` instead, until an attempt succeeds.
        """
        async with self._discovery_lock:
            return await self._discover()

    async def _discover(self) -> bool:
        """One attempt. Callers hold `_discovery_lock`."""
        if self._job_ids_explicit:
            self.job_ids_source = "config"
            return False

        tag = self.config.project_tag
        if self.jobs_api is None:
            self.degraded["job_ids"] = (
                "no DBX_JOB_IDS configured and no Jobs API to discover them from; "
                "no model can be triggered from this app"
            )
            log.warning(self.degraded["job_ids"])
            return False

        self._discovery_attempted_at = time.monotonic()
        try:
            # Bounded: startup must not hang on a slow or wedged workspace,
            # and neither may a trigger waiting on an on-demand refresh.
            jobs = await asyncio.wait_for(self.jobs_api.list_jobs(), timeout=30)
        except Exception as exc:  # noqa: BLE001 - a failed lookup is degraded, not fatal
            self._discovery_failed(f"discovering jobs failed ({exc})", exc_info=True)
            return False

        found = map_jobs_to_models(jobs, tag)
        if not found.job_ids:
            # Treated as a failure, not as "there are now no models". The
            # realistic causes of a previously non-empty answer going empty
            # all at once are a principal that lost access or a tag edited
            # on the wrong side — both mistakes a stale map survives better
            # than an empty one. A single job disappearing from a non-empty
            # answer IS honoured, below.
            self._discovery_failed(
                f"none of the {len(jobs)} jobs visible to this app are tagged "
                f"project={tag} or named '... {tag} · <model>'"
            )
            return False

        previous = self.config.job_ids
        self.config = replace(self.config, job_ids=found.job_ids)
        self.job_ids_source = "discovered"
        self.discovery.refreshes += 1
        self.discovery.last_success_at = _now()
        self.degraded.pop("job_ids", None)
        self.degraded.pop("job_discovery", None)

        if found.job_ids != previous:
            added = sorted(set(found.job_ids) - set(previous))
            removed = sorted(set(previous) - set(found.job_ids))
            log.info(
                "job discovery (project=%s): %d job(s): %s%s%s",
                tag,
                len(found.job_ids),
                ", ".join(f"{m}={j}" for m, j in found.job_ids.items()),
                f"; added {', '.join(added)}" if previous and added else "",
                f"; removed {', '.join(removed)}" if removed else "",
            )

        if found.ambiguous:
            ambiguous = (
                "more than one job claims the same model, so the highest id won: "
                + "; ".join(f"{m}: {ids}" for m, ids in found.ambiguous.items())
            )
            if self.degraded.get("job_ids_ambiguous") != ambiguous:
                log.warning(ambiguous)
            self.degraded["job_ids_ambiguous"] = ambiguous
        else:
            self.degraded.pop("job_ids_ambiguous", None)
        return True

    def _discovery_failed(self, reason: str, *, exc_info: bool = False) -> None:
        """Record a failed attempt without discarding what a good one found."""
        self.discovery.failures += 1
        self.discovery.last_error = reason
        self.discovery.last_error_at = _now()

        if self.job_ids_source == "discovered" and self.config.job_ids:
            self.degraded["job_discovery"] = (
                f"the last job discovery failed: {reason}. Still using the "
                f"{len(self.config.job_ids)} job(s) found at "
                f"{self.discovery.last_success_at}"
            )
            log.warning(self.degraded["job_discovery"], exc_info=exc_info)
            return

        self.degraded["job_ids"] = (
            f"no DBX_JOB_IDS configured and {reason}; no model can be triggered from this app"
        )
        log.warning(self.degraded["job_ids"], exc_info=exc_info)

    def _start_discovery_refresh(self) -> None:
        """Start the periodic refresh, if there is anything for it to do.

        Not when the map is explicit (it is an allow-list; refreshing would
        widen it), not without a Jobs API, and not when the interval is 0.
        Started even when the startup attempt failed — recovering from that
        without a restart is half of why the refresh exists.
        """
        interval = self.config.discovery_refresh_s
        if self._job_ids_explicit or self.jobs_api is None or interval <= 0:
            self.discovery.refresh_s = 0.0
            return
        self.discovery.refresh_s = interval
        self._discovery_task = asyncio.create_task(
            self._discovery_loop(interval), name="job-discovery-refresh"
        )

    async def _discovery_loop(self, interval: float) -> None:
        """Re-discover every `interval` seconds until cancelled.

        This calls the Jobs API and nothing else. It must never grow a SQL
        warehouse query: warehouse cost is uptime, and a loop touching it
        every few minutes would keep it awake all day.
        """
        while True:
            await asyncio.sleep(interval)
            try:
                await self.refresh_job_ids()
            except Exception:  # noqa: BLE001 - the loop outlives any one bad attempt
                log.exception("job discovery refresh raised; retrying in %ss", interval)

    async def _stop_discovery_refresh(self) -> None:
        task, self._discovery_task = self._discovery_task, None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def job_id_for(self, model: str) -> int | None:
        """Which job runs `model` — refreshing discovery once if it is unknown.

        A job created after the last refresh would otherwise 404 for up to
        `discovery_refresh_s`. So an unknown name costs one refresh, then the
        answer stands. Rate-limited by `discovery_on_demand_min_s` across ALL
        callers, so a client naming nonexistent models cannot turn this into
        a paged walk of the workspace's job list per request.

        Never refreshes an explicit map: that is an allow-list, and a model
        missing from it is missing on purpose.
        """
        job_id = self.config.job_id_for(model)
        if job_id is not None or self._job_ids_explicit or self.jobs_api is None:
            return job_id

        async with self._discovery_lock:
            # Someone else's refresh may have found it while this one waited.
            job_id = self.config.job_id_for(model)
            if job_id is not None:
                return job_id
            last = self._discovery_attempted_at
            if last is not None and (
                time.monotonic() - last < self.config.discovery_on_demand_min_s
            ):
                return None
            await self._discover()
        return self.config.job_id_for(model)

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
        """Connect to Lakebase once, CHECK its schema, and say what happened.

        **Nothing is created here.** The DDL (`lakebase_ddl/`) is applied out
        of band — `docs/v5-implementation-plan.md`, Phase 2 item 3. A missing
        or mismatched table is reported as `lakebase_schema` degraded, with
        the reason, and the store stays None: every route needing it answers a
        clean 503 carrying that reason, instead of a 500 from a failed query.
        The check runs once, so applying the DDL means restarting the app.
        """
        if not cfg.lakebase_dsn:
            self.degraded["store"] = (
                "no run store: Lakebase is not configured. Triggering and streaming "
                "still work; listing and reading past runs do not, because that is "
                "where their status is recorded. See DBX_LAKEBASE_* in resources/app.yml"
            )
            log.warning(self.degraded["store"])
            return

        self._check_lakebase_identity(cfg)
        store = PostgresRunStore(
            cfg.lakebase_dsn,
            schema=cfg.lakebase_schema,
            password_provider=self.token_provider,
        )
        try:
            problems = await store.check_schema()
        except Exception as exc:  # noqa: BLE001
            self.degraded["lakebase"] = f"Lakebase configured but unreachable: {exc}"
            log.error(self.degraded["lakebase"])
            return

        version = store.server_version or "version unknown"
        if problems:
            self.degraded["lakebase_schema"] = (
                f"Lakebase is reachable (postgres {version}) but its schema is not what "
                f"this app expects, so run state is unavailable: {'; '.join(problems)}. "
                "The app creates no tables: apply lakebase_ddl/001_run_status.sql and "
                "lakebase_ddl/002_run_status_history.sql (deploy/README.md), then "
                "restart the app"
            )
            log.error(self.degraded["lakebase_schema"])
            return

        self.store = store
        log.info("run store: Lakebase (postgres %s), schema %s checked", version, store.schema)

    async def shutdown(self) -> None:
        await self._stop_discovery_refresh()
        if self.jobs_api is not None:
            await self.jobs_api.close()
        if self.store is not None:
            await self.store.close()

    async def ingest(self, run_id: str, msg: Message) -> None:
        """One entry point for everything arriving from a job, whichever
        channel it came in on. WS and HTTP push must not diverge."""
        self.messages_ingested += 1
        await self.broadcaster.publish(run_id, msg)
        # No write to `run_status` here, and that is the point of v5 Phase 2.
        # The JOB writes its own status row (job/lakebase.py, from the
        # harness's controller thread) for every run, observed or not — so
        # the app is a reader of that table and never a second writer to the
        # same row. A status message arriving here is a notification for the
        # SSE stream, nothing more.


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
