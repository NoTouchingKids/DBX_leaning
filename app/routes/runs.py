"""Run-level operations: status, backfill, cancel.

Every read here is client-triggered. Nothing in this module runs on a timer.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query, Request, status

from shared.protocol import cancel as cancel_frame

from ..deps import Hub, Repo

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


@router.get("/{run_id}")
async def get_run(run_id: str, repo: Repo, hub: Hub) -> dict:
    row = await repo.run_status(run_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no such run {run_id}")
    snapshot = hub.broadcaster.snapshot(run_id)
    return {
        "run": row,
        "live": hub.jobs.is_connected(run_id),
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
    delivered = await hub.jobs.send(run_id, cancel_frame(run_id, requested_by=requested_by))
    if not delivered:
        raise HTTPException(status.HTTP_409_CONFLICT, CANCEL_ESCAPE_HATCH)
    log.info("cancel for %s forwarded (requested by %s)", run_id, requested_by)
    return {"run_id": run_id, "cancel_requested": True, "requested_by": requested_by}
