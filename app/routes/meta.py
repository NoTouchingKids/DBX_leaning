"""Health and identity.

``whoami`` is cosmetic. The platform proxy already authenticated the caller;
this endpoint tells client code who that is, for display and attribution. It
is **not** an authorization boundary — that comes from Unity Catalog grants.
There is no on-behalf-of-user path in this build at all.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..deps import Hub

router = APIRouter(tags=["meta"])

#: Headers the Databricks Apps proxy forwards about the authenticated user.
_IDENTITY_HEADERS = (
    ("email", "x-forwarded-email"),
    ("user", "x-forwarded-preferred-username"),
    ("user_id", "x-forwarded-user"),
)


@router.get("/healthz")
async def healthz(hub: Hub) -> dict:
    return {
        "status": "degraded" if hub.degraded else "ok",
        "degraded": hub.degraded,
        "live_jobs": len(hub.jobs.run_ids),
        "messages_ingested": hub.messages_ingested,
    }


@router.get("/api/whoami")
async def whoami(request: Request, hub: Hub) -> dict:
    identity = {
        key: request.headers.get(header) for key, header in _IDENTITY_HEADERS
    }
    return {
        **identity,
        "authenticated": any(identity.values()),
        # Said out loud so nobody builds an authorization check on this.
        "note": "cosmetic identity from the platform proxy; not an authorization boundary",
    }
