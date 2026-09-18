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
from .routes import meta, rpc, runs, stream
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

    # There is no startup reconciliation here any more, and nothing replaces
    # it. It read `run_events` from the SQL warehouse to repair run state a
    # run had drifted out of while the app was down — and in v4 there is no
    # drift to repair: the JOB keeps its own `run_status` row current in
    # Lakebase whether or not anything is listening, and "is it running?" is
    # answered by the Jobs API, which cannot go stale by construction.
    #
    # Worth noting what that retires, because it was load-bearing: a job that
    # died before emitting a terminal status used to hold one of five
    # account-wide slots forever, and reconciliation was the only way back
    # short of editing the table by hand. A job that owns its own row does not
    # create that state.

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
    app.include_router(rpc.router)
    app.include_router(stream.router)
    app.include_router(runs.router)

    # Last, and it has to stay last: the SPA fallback is a catch-all, and
    # Starlette returns the first route that matches. Registered before the
    # routers it would answer /api/runs with index.html and a 200.
    app.state.spa_built = mount_spa(app, cfg.frontend_dist)
    return app


app = create_app()
