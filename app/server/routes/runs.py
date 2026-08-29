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

from ..deps import Hub, Repo, Store, get_repo
from ..jobs_api import JobsApiError
from ..repository import UnsafeTableName, validate_table_name
from ..services import BACKFILL_TIMEOUT_S
from ..store import DuplicateRun, SlotDenied

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/runs", tags=["runs"])

#: The most this will ask a job for in one backfill reply. It mirrors
#: ``DEFAULT_BACKFILL_LIMIT`` in ``job/bus.py``, where the job caps its own
#: reply, and it must never exceed it.
#:
#: The reason is ``more``. A page shorter than what was asked for reads as
#: "that is all there is", so asking a job for 5,000 and being handed its
#: capped 500 would tell a client to stop paging in the middle of its gap —
#: messages silently missing, with a `more: false` saying there are none. Ask
#: for no more than the job will send and a short page means what it says.
#: ``tests/app/test_backfill_routing.py`` pins the two together.
JOB_REPLY_LIMIT = 500

#: What an operator does when a job is running with no live channel to it.
#: Documented rather than built around — the app cannot reach a job it has no
#: socket to, and a status table polled on a timer is not an acceptable
#: substitute.
CANCEL_ESCAPE_HATCH = (
    "no live channel to this run; cancel it with "
    "`databricks jobs cancel-run --run-id <job_run_id>` (a hard kill: the job "
    "gets SIGTERM and keeps whatever results it already wrote)"
)


#: Every parameter the trigger endpoint may send. A Databricks job rejects a
#: run-now parameter it has not declared, so each job in resources/*.job.yml
#: must declare exactly these — tests/deploy/test_bundle.py fails if the two
#: drift apart.
JOB_PARAMETER_NAMES = frozenset(
    {
        "DBX_RUN_ID",
        "DBX_MODEL",
        "DBX_MODEL_CONFIG",
        "DBX_CATALOG",
        "DBX_SCHEMA",
        "DBX_APP_URL",
        "DBX_APP_TOKEN",
    }
)


def build_job_parameters(
    run_id: str, model: str, config: dict[str, Any], app_config: Any
) -> dict[str, str]:
    """What the job is launched with. Every key must be in JOB_PARAMETER_NAMES."""
    parameters = {
        "DBX_RUN_ID": run_id,
        "DBX_MODEL": f"job.models.{model}",
        "DBX_MODEL_CONFIG": json.dumps(config),
        "DBX_CATALOG": app_config.catalog,
        "DBX_SCHEMA": app_config.schema,
    }
    if app_config.public_url:
        # Where to attach. Absent is fine — the run proceeds unobserved.
        parameters["DBX_APP_URL"] = app_config.public_url
    if app_config.job_token:
        parameters["DBX_APP_TOKEN"] = app_config.job_token
    return parameters


class TriggerRequest(BaseModel):
    """What the client sends to start a run."""

    model: str = Field(min_length=1, description="a key of DBX_JOB_IDS")
    #: Passed through to the model's factory verbatim as DBX_MODEL_CONFIG.
    config: dict[str, Any] = Field(default_factory=dict)
    #: Supply one to make the trigger idempotent from the caller's side;
    #: otherwise the app mints it.
    run_id: str | None = None


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def trigger_run(body: TriggerRequest, request: Request, store: Store, hub: Hub) -> dict:
    """Launch a model as a Databricks Job, and register the run.

    **Reserve, then launch, then attach** — in that order. Registering after
    the launch left a window where the job was running and the registry did
    not know: nothing could list it, and startup reconciliation, which finds
    work by reading that table, would never see it. Claiming the slot first
    also makes the concurrency ceiling real rather than advisory, because on
    Lakebase the count and the insert happen in one transaction.
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

    run_id = body.run_id or f"run-{uuid.uuid4().hex[:12]}"
    requested_by = request.headers.get("x-forwarded-email")

    # Free Edition allows 5 concurrent job tasks per account, across all
    # models. Refusing here with a clear reason beats Databricks queueing or
    # rejecting the run somewhere the user cannot see.
    try:
        await store.claim_slot(
            run_id,
            model=body.model,
            ceiling=hub.config.max_concurrent_runs,
            requested_by=requested_by,
        )
    except SlotDenied as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from exc
    except DuplicateRun as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - the store being down is not a crash
        # Nothing has been launched yet, so this is a clean refusal: better an
        # honest 503 than an orphan job the registry never heard of.
        log.exception("could not claim a slot for %s", run_id)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"could not register the run, so nothing was launched: {exc}",
        ) from exc

    parameters = build_job_parameters(run_id, body.model, body.config, hub.config)

    try:
        job_run_id = await hub.jobs_api.run_now(job_id, parameters)
    except JobsApiError as exc:
        # Nothing was launched, so give the slot back rather than leaving a
        # phantom holding a place in the ceiling.
        await store.release_slot(run_id)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    try:
        await store.attach_job_run(run_id, job_run_id)
        attached = True
    except Exception:  # noqa: BLE001 - the job is running; that is what matters
        log.exception(
            "run %s launched as job run %s but the id could not be stored", run_id, job_run_id
        )
        attached = False

    log.info("triggered %s as job run %s (model=%s)", run_id, job_run_id, body.model)
    return {
        "run_id": run_id,
        "job_run_id": job_run_id,
        "model": body.model,
        "status": "QUEUED",
        "registered": True,
        "job_run_id_stored": attached,
        "stream": f"/api/runs/{run_id}/stream",
    }


@router.get("")
async def list_runs(
    store: Store,
    hub: Hub,
    limit: int = Query(50, ge=1, le=500),
    status_filter: str | None = Query(None, alias="status"),
    model: str | None = Query(None, description="narrow to one model, e.g. mcmc"),
) -> dict:
    """Recent runs, newest first, optionally filtered.

    Filtering server-side rather than letting the client sieve the top-N
    window: that only works while the window happens to be big enough to hold
    everything relevant, and stops working silently when it is not.
    """
    runs = await store.list_runs(limit=limit, status=status_filter, model=model)
    live = set(hub.job_sockets.run_ids)
    return {
        "count": len(runs),
        "filters": {"status": status_filter, "model": model},
        "runs": [{**r.as_dict(), "live": r.run_id in live} for r in runs],
    }


@router.get("/{run_id}")
async def get_run(run_id: str, store: Store, hub: Hub) -> dict:
    record = await store.get(run_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no such run {run_id}")
    snapshot = hub.broadcaster.snapshot(run_id)
    return {
        "run": record.as_dict(),
        "live": hub.job_sockets.is_connected(run_id),
        "last_seq_seen": snapshot.last_seq if snapshot else None,
    }


def _backfill_page(
    run_id: str,
    after_seq: int,
    messages: list[dict[str, Any]],
    *,
    page: int,
    source: str,
) -> dict:
    """One response shape, whichever source answered.

    A client cannot be made to care where its messages came from: the same
    envelope-shaped dicts, paged the same way by ``next_after_seq``. ``source``
    is the single exception, and it is here because "did that read wake the
    warehouse" is the question this whole routing exists to answer, and it
    should be observable in a response rather than only in the app's log.
    """
    return {
        "run_id": run_id,
        "after_seq": after_seq,
        "count": len(messages),
        "messages": messages,
        # A full page probably means there is more; the client pages by seq.
        "more": len(messages) >= page,
        "next_after_seq": messages[-1]["seq"] if messages else after_seq,
        "source": source,
    }


@router.get("/{run_id}/messages")
async def backfill(
    run_id: str,
    hub: Hub,
    after_seq: int = Query(-1, ge=-1, description="exclusive lower bound on seq"),
    limit: int | None = Query(None, ge=1, le=50_000),
) -> dict:
    """Explicit, client-triggered backfill: the job first, Unity Catalog if it
    cannot answer.

    Called when a client knows it has a real gap — not automatically on every
    reconnect. A finished run is immutable, so a client that has fetched one
    can cache it forever.

    **Ask the job first, and only then SQL.** A tab backgrounded for thirty
    seconds reconnects with a gap of a few dozen messages. Served from Unity
    Catalog that is a SELECT, which means the warehouse is awake, which means
    flicking between tabs is a way to spend money — warehouse cost is uptime,
    not statement count. Served from the job's replay ring it is one WebSocket
    frame and no warehouse at all.

    Where the boundary sits is a fact on the wire, not a constant kept here:
    the job states the oldest seq it can still replay, and a gap that reaches
    below it is the warehouse's to answer.

    The job's other bound, `flushed_through_seq`, needs no branch of its own.
    What Delta cannot serve yet is the newest messages, and those are always
    inside the ring — so a client paging up from below the floor crosses into
    the job's range on a later page and the job answers exactly those.
    """
    page = limit or hub.config.backfill_page_size

    if hub.job_sockets.can_serve(run_id, after_seq):
        asked = min(page, JOB_REPLY_LIMIT)
        reply = await hub.job_sockets.backfill(
            run_id, after_seq=after_seq, limit=asked, timeout_s=BACKFILL_TIMEOUT_S
        )
        if reply is not None and reply.get("complete"):
            messages = [m for m in reply.get("messages") or [] if isinstance(m, dict)]
            return _backfill_page(run_id, after_seq, messages, page=asked, source="job")
        if reply is not None:
            # `complete: false` — the ring evicted past this cursor between
            # the bounds we had and the reply. Its partial page is discarded
            # rather than stitched onto the SQL one: two sources merged into a
            # single page is a dedupe problem, and the warehouse can serve the
            # whole gap on its own.
            log.info(
                "job for %s could not cover a backfill after seq %d (replayable from %s); "
                "reading it from the warehouse",
                run_id,
                after_seq,
                reply.get("replay_from_seq"),
            )

    # No socket, no answer, or not the whole answer. Only *now* is the read
    # path required, which is why `repo` is resolved here rather than injected:
    # a deployment with Lakebase and no warehouse can still serve a live run's
    # recent gap, and must not be 503'd for a question the job just answered.
    repo = get_repo(hub)
    messages = await repo.messages_since(run_id, after_seq, page)
    return _backfill_page(run_id, after_seq, messages, page=page, source="warehouse")


@router.get("/{run_id}/results")
async def read_results(
    run_id: str,
    repo: Repo,
    hub: Hub,
    limit: int = Query(1000, ge=1, le=50_000),
    offset: int = Query(0, ge=0),
) -> dict:
    """The full result set a `result` message only previews.

    The envelope deliberately carries a bounded preview and a `fetch_hint`
    rather than the rows themselves — the data lives in the model's own table,
    under its own grants. This is the endpoint that hint points at: a browser
    cannot query Unity Catalog, so without it the "pull the full set on
    demand" half of the contract does not exist.

    Client-triggered, like backfill. Nothing here runs on a timer.
    """
    table = await repo.results_table_for(run_id)
    if table is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"no results recorded for {run_id}; the run may not have reached results yet",
        )

    try:
        # A table name is an identifier, not a value, so it cannot be bound —
        # this is the gate that stands in for that.
        table = validate_table_name(table, catalog=hub.config.catalog, schema=hub.config.schema)
    except UnsafeTableName as exc:
        log.error("refusing results read for %s: %s", run_id, exc)
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc

    rows = await repo.read_results(table, run_id, limit=limit, offset=offset)
    return {
        "run_id": run_id,
        "table": table,
        "count": len(rows),
        "offset": offset,
        "rows": rows,
        # A full page probably means more; the client pages by offset.
        "more": len(rows) >= limit,
        "next_offset": offset + len(rows),
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
