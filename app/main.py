"""FastAPI application factory.

Everything long-lived is built in ``lifespan`` and stored on ``app.state``,
never at module level. Routes reach it through ``Depends``, so a service that
failed to start produces a 503 rather than an AttributeError.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .config import AppConfig
from .jobs_api import JobsApi
from .reconcile import reconcile_once
from .routes import ingest, meta, runs, stream
from .services import ServiceHub
from .spa import mount_spa

log = logging.getLogger(__name__)

__all__ = ["create_app"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    hub: ServiceHub | None = getattr(app.state, "hub", None)
    if hub is None:
        # Nothing to start. Routes will answer 503 through Depends rather
        # than failing the whole process on the way up.
        log.error("no ServiceHub on app.state; the app will serve 503s")
        yield
        return

    await hub.startup()

    if hub.config.reconcile_on_startup and hub.store is not None:
        # Once, on the way up. A job that started or finished while the app
        # was down is the normal case here — apps run ~8h/day, jobs do not.
        # There is deliberately no periodic version of this.
        #
        # Gated on `store`, NOT on `repo`. It was gated on `repo` when
        # `run_status` lived in the warehouse; after the move to Lakebase that
        # left a deploy with Postgres and no warehouse — the recommended
        # shape — silently never reconciling. Every job that died before
        # emitting a status then held one of five account-wide slots forever,
        # so five bad configs took the platform down with no way back short
        # of editing the table by hand. `repo` is now optional and only
        # sharpens the answer.
        jobs = JobsApi(hub.config.workspace_host, hub.config.token)
        try:
            report = await reconcile_once(hub.repo, jobs, hub.store)
            log.info("startup reconciliation: %r", report)
        except Exception:  # noqa: BLE001 - never block startup on this
            log.exception("startup reconciliation failed")
        finally:
            await jobs.close()

    try:
        yield
    finally:
        await hub.shutdown()


def create_app(config: AppConfig | None = None) -> FastAPI:
    cfg = config or AppConfig.from_env()
    app = FastAPI(
        title="DBX_leaning",
        version="0.1.0",
        summary="Trigger and observe long-running models on Databricks",
        lifespan=lifespan,
    )
    app.state.hub = ServiceHub(cfg)

    app.include_router(meta.router)
    app.include_router(ingest.router)
    app.include_router(stream.router)
    app.include_router(runs.router)

    # Last, and it has to stay last: the SPA fallback is a catch-all, and
    # Starlette returns the first route that matches. Registered before the
    # routers it would answer /api/runs with index.html and a 200.
    app.state.spa_built = mount_spa(app, cfg.frontend_dist)
    return app


app = create_app()
