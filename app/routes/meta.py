"""Health and identity.

``whoami`` is cosmetic. The platform proxy already authenticated the caller;
this endpoint tells client code who that is, for display and attribution. It
is **not** an authorization boundary — that comes from Unity Catalog grants.
There is no on-behalf-of-user path in this build at all.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status

from shared.schema import SCHEMA_VERSION, control_schema, envelope_schema, protocol_schema

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
        "protocol_schema_version": SCHEMA_VERSION,
        "degraded": hub.degraded,
        "live_jobs": len(hub.job_sockets.run_ids),
        "messages_ingested": hub.messages_ingested,
    }


@router.get("/api/schema")
async def schema(kind: str = "protocol") -> dict:
    """The wire protocol as JSON Schema, for the client to type itself from.

    Served as well as committed under ``schema/``: the file is what a build
    step generates TypeScript from, and this is what a running client can
    check it is actually talking to — a version mismatch between a cached
    bundle and a redeployed app is otherwise invisible until something
    silently fails to parse.
    """
    builders = {
        "protocol": protocol_schema,
        "envelope": envelope_schema,
        "control": control_schema,
    }
    build = builders.get(kind)
    if build is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"no schema {kind!r}; available: {', '.join(sorted(builders))}",
        )
    return build()


@router.get("/api/models")
async def models(hub: Hub) -> dict:
    """What can be triggered from here.

    Derived from the configured job map, not from importing ``models/`` — the
    app has no business pulling in gurobipy, scikit-learn and emcee just to
    list names, and a model with no job behind it cannot be run anyway.
    """
    return {
        "models": [
            {"name": name, "job_id": hub.config.job_ids[name]}
            for name in hub.config.triggerable_models
        ],
        "default_job_id": hub.config.default_job_id,
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
