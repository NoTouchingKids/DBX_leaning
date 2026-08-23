"""Dependency injection off ``request.app.state``.

The point of resolving services this way rather than through a
``get_services()`` that assumes initialisation: a dependency function can say
"this is not available right now" as a clean 503. A missing service should
never surface as an AttributeError.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from .broadcaster import Broadcaster
from .config import AppConfig
from .repository import RunRepository
from .services import ServiceHub

__all__ = ["get_hub", "get_broadcaster", "get_repo", "get_config", "Hub", "Repo", "Caster"]


def get_hub(request: Request) -> ServiceHub:
    hub = getattr(request.app.state, "hub", None)
    if hub is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "application services are not initialised"
        )
    return hub


def get_config(hub: Annotated[ServiceHub, Depends(get_hub)]) -> AppConfig:
    return hub.config


def get_broadcaster(hub: Annotated[ServiceHub, Depends(get_hub)]) -> Broadcaster:
    return hub.broadcaster


def get_repo(hub: Annotated[ServiceHub, Depends(get_hub)]) -> RunRepository:
    if hub.repo is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            hub.degraded.get("sql", "the read path is unavailable"),
        )
    return hub.repo


Hub = Annotated[ServiceHub, Depends(get_hub)]
Repo = Annotated[RunRepository, Depends(get_repo)]
Caster = Annotated[Broadcaster, Depends(get_broadcaster)]
