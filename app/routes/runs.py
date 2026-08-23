"""Run-level operations: status, backfill, cancel.

Every read here is client-triggered. Nothing in this module runs on a timer.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from shared.protocol import cancel as cancel_frame

from ..deps import Hub, Repo
from ..jobs_api import JobsApiError

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/runs", tags=["runs"])

#: What an operator does when a job is running with no live channel to it.
#: Documented rather than built around — the app cannot reach a job it has no
#: socket to, and a status table polled on a timer is not an acceptable
#: substitute.
CANCEL_ESCAPE_HATCH = (
    "no live channel to this run; cancel it with "
    "`databricks jobs cancel-run --run-id <job_run_id>` (a hard kill: the job "
    "gets SIGTERM and keeps whatever results it already wrote)"
)


class TriggerRequest(BaseModel):
    """What the client sends to start a run."""

    model: str = Field(min_length=1, description="a key of DBX_JOB_IDS")
    #: Passed through to the model's factory verbatim as DBX_MODEL_CONFIG.
    config: dict[str, Any] = Field(default_factory=dict)
    #: Supply one to make the trigger idempotent from the caller's side;
    #: otherwise the app mints it.
    run_id: str | None = None


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def trigger_run(body: TriggerRequest, request: Request, repo: Repo, hub: Hub) -> dict:
    """Launch a model as a Databricks Job, and register the run.

    The registration is the part that matters beyond the launch: without a
    ``run_status`` row nothing can list this run, and startup reconciliation —
    which finds work by reading that table — would never see it.
    """
    if hub.jobs_api is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            hub.degraded.get("jobs_api", "runs cannot be triggered from this app"),
        )

    job_id = hub.config.job_id_for(body.model)
    if job_id is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"no job configured for model {body.model!r}; "
            f"triggerable models are {hub.config.triggerable_models or '(none)'}",
        )

    # Free Edition allows 5 concurrent job tasks per account, across all
    # models. Refusing here with a clear reason beats Databricks queueing or
    # rejecting the run somewhere the user cannot see.
    active = await repo.active_run_count()
    if active >= hub.config.max_concurrent_runs:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"{active} runs already active and the account ceiling is "
            f"{hub.config.max_concurrent_runs} concurrent job tasks; wait for one to finish",
        )

    run_id = body.run_id or f"run-{uuid.uuid4().hex[:12]}"
    requested_by = request.headers.get("x-forwarded-email")

    parameters = {
        "DBX_RUN_ID": run_id,
        "DBX_MODEL": f"models.{body.model}",
        "DBX_MODEL_CONFIG": json.dumps(body.config),
        "DBX_CATALOG": hub.config.catalog,
        "DBX_SCHEMA": hub.config.schema,
    }
    if hub.config.public_url:
        # Where to attach. Absent is fine — the run proceeds unobserved.
        parameters["DBX_APP_URL"] = hub.config.public_url
    if hub.config.job_token:
        parameters["DBX_APP_TOKEN"] = hub.config.job_token

    try:
        job_run_id = await hub.jobs_api.run_now(job_id, parameters)
    except JobsApiError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    try:
        await repo.create_run(
            run_id, model=body.model, job_run_id=job_run_id, requested_by=requested_by
        )
    except Exception:
        # The job is already running — losing the registry row must not read
        # as "nothing happened". Return the ids so the caller can still watch.
        log.exception("run %s launched as job run %s but could not be registered",
                      run_id, job_run_id)
        return {
            "run_id": run_id,
            "job_run_id": job_run_id,
            "model": body.model,
            "registered": False,
            "warning": "the job is running but run_status could not be written; "
                       "startup reconciliation will not see this run",
        }

    log.info("triggered %s as job run %s (model=%s)", run_id, job_run_id, body.model)
    return {
        "run_id": run_id,
        "job_run_id": job_run_id,
        "model": body.model,
        "status": "QUEUED",
        "registered": True,
        "stream": f"/api/runs/{run_id}/stream",
    }


@router.get("")
async def list_runs(
    repo: Repo,
    hub: Hub,
    limit: int = Query(50, ge=1, le=500),
    status_filter: str | None = Query(None, alias="status"),
) -> dict:
    runs = await repo.list_runs(limit=limit, status=status_filter)
    live = set(hub.job_sockets.run_ids)
    return {
        "count": len(runs),
        "runs": [{**row, "live": row["run_id"] in live} for row in runs],
    }


@router.get("/{run_id}")
async def get_run(run_id: str, repo: Repo, hub: Hub) -> dict:
    row = await repo.run_status(run_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no such run {run_id}")
    snapshot = hub.broadcaster.snapshot(run_id)
    return {
        "run": row,
        "live": hub.job_sockets.is_connected(run_id),
        "last_seq_seen": snapshot.last_seq if snapshot else None,
    }


@router.get("/{run_id}/messages")
async def backfill(
    run_id: str,
    repo: Repo,
    hub: Hub,
    after_seq: int = Query(-1, ge=-1, description="exclusive lower bound on seq"),
    limit: int | None = Query(None, ge=1, le=50_000),
) -> dict:
    """Explicit, client-triggered backfill from Unity Catalog.

    Called when a client knows it has a real gap — not automatically on every
    reconnect. A finished run is immutable, so a client that has fetched one
    can cache it forever.
    """
    page = limit or hub.config.backfill_page_size
    messages = await repo.messages_since(run_id, after_seq, page)
    return {
        "run_id": run_id,
        "after_seq": after_seq,
        "count": len(messages),
        "messages": messages,
        # A full page probably means there is more; the client pages by seq.
        "more": len(messages) >= page,
        "next_after_seq": messages[-1]["seq"] if messages else after_seq,
    }


@router.post("/{run_id}/cancel")
async def cancel_run(run_id: str, request: Request, hub: Hub) -> dict:
    """Forward a cancel to the job over its WebSocket, if one is live.

    This is the only inbound path to a running job. There is no fallback that
    polls a table for a cancel flag: that would keep the SQL warehouse awake
    for the whole run.
    """
    requested_by = request.headers.get("x-forwarded-email") or "unknown"
    delivered = await hub.job_sockets.send(run_id, cancel_frame(run_id, requested_by=requested_by))
    if not delivered:
        raise HTTPException(status.HTTP_409_CONFLICT, CANCEL_ESCAPE_HATCH)
    log.info("cancel for %s forwarded (requested by %s)", run_id, requested_by)
    return {"run_id": run_id, "cancel_requested": True, "requested_by": requested_by}
