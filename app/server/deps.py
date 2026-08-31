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
from .services import ServiceHub
from .store import PostgresRunStore

__all__ = [
    "get_hub",
    "get_broadcaster",
    "get_store",
    "get_config",
    "Hub",
    "Store",
    "Caster",
]


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


def get_store(hub: Annotated[ServiceHub, Depends(get_hub)]) -> PostgresRunStore:
    if hub.store is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            hub.degraded.get("store") or hub.degraded.get("lakebase") or "no run store",
        )
    return hub.store


Hub = Annotated[ServiceHub, Depends(get_hub)]
Store = Annotated[PostgresRunStore, Depends(get_store)]
Caster = Annotated[Broadcaster, Depends(get_broadcaster)]
