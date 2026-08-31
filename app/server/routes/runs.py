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

from shared.rpc import Method, RpcError

from ..deps import Hub, Store
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

    model: str = Field(
        min_length=1,
        description=(
            "A model the app has discovered — e.g. 'heartbeat'. Jobs are found "
            "in the workspace by their `project: dbx-leaning` tag, so this is "
            "whatever GET /api/models lists, not a key of any configured map."
        ),
        examples=["heartbeat"],
    )
    #: Passed through to the model's factory verbatim as DBX_MODEL_CONFIG.
    config: dict[str, Any] = Field(
        default_factory=dict,
        description="Model-specific. The heartbeat takes {'seconds': int, 'hz': float}.",
        examples=[{"seconds": 120, "hz": 1}],
    )
    #: Supply one to make the trigger idempotent from the caller's side;
    #: otherwise the app mints it. Worth supplying when testing by hand: it is
    #: what you put in the client's `?run=` to watch the run you just started.
    run_id: str | None = Field(default=None, examples=["hb-001"])


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def trigger_run(body: TriggerRequest, request: Request, hub: Hub) -> dict:
    """Launch a model as a Databricks Job. That is the whole route.

    **It does not touch the run store, and that is the change from v3.** v3
    reserved a slot, launched, then attached the job run id — count-and-insert
    in one transaction, so the 5-task ceiling was "real rather than advisory".

    Two things retire that, and both come from a job being a service rather
    than a subroutine:

    - **A scheduled run never passes through here at all**, so a ceiling
      enforced only on this path was already counting the wrong number. The
      authority on what is running is the Jobs API, which cannot drift.
    - **The job writes its own `run_status` row** and keeps it current whether
      or not this app is up — so registering the run here would be the app
      writing a record it does not own, on the one code path that happens to
      pass through it.

    The ceiling still holds; Databricks holds it. Every job file sets
    `queue.enabled`, so a sixth concurrent task waits instead of failing.

    The upshot worth knowing when testing: **triggering needs no Lakebase.**
    Only listing and reading runs do.
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
            f"no job found for model {body.model!r}; discovered models are "
            f"{hub.config.triggerable_models or '(none — check the project tag and /healthz)'}",
        )

    run_id = body.run_id or f"run-{uuid.uuid4().hex[:12]}"
    parameters = build_job_parameters(run_id, body.model, body.config, hub.config)

    try:
        job_run_id = await hub.jobs_api.run_now(job_id, parameters)
    except JobsApiError as exc:
        # Nothing to unwind: no slot was claimed and no row was written.
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    log.info("triggered %s as job run %s (model=%s)", run_id, job_run_id, body.model)
    return {
        "run_id": run_id,
        "job_run_id": job_run_id,
        "model": body.model,
        # QUEUED is what Databricks has accepted, not something we recorded.
        # The job writes the authoritative row when it starts.
        "status": "QUEUED",
        "stream": f"/api/runs/{run_id}/stream",
        "watch": f"/?run={run_id}",
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


# The two warehouse-backed reads that used to live here — `GET
# /{run_id}/messages` (backfill) and `GET /{run_id}/results` — are gone, and
# neither is coming back in this shape. See docs/v4-rewrite-plan.md.
#
# Backfill is now a `replay(from_seq, to_seq)` call the app makes to the JOB
# over its RPC channel: the job re-reads its own telemetry log and resends the
# gap. The app cannot serve it from files even if it wanted to — it holds no
# grant on the telemetry volume, deliberately (uc_ddl/004_telemetry_volume.sql).
#
# Results were never the app's to serve either, now that a model writes its own
# results table. History for a FINISHED run comes from SQL once the ingestion
# job has loaded it, which is Slice 4.


@router.post("/{run_id}/cancel")
async def cancel_run(run_id: str, request: Request, hub: Hub) -> dict:
    """Ask the job to cancel, and report what it said.

    The only inbound path to a running job, and the first thing the RPC
    interface buys that v3 could not: v3 sent a fire-and-forget frame and
    answered the caller optimistically, so "the job never got it" and "the job
    accepted it" looked identical from here.

    There is still no fallback that polls a table for a cancel flag. That would
    keep the SQL warehouse awake for the whole run, which is the cost mistake
    this platform was built to avoid — and there is no warehouse on the app's
    path any more anyway.
    """
    requested_by = request.headers.get("x-forwarded-email") or "unknown"
    try:
        result = await hub.job_sockets.call(run_id, Method.CANCEL, {"requested_by": requested_by})
    except ConnectionError as exc:
        # No job attached, or it went away mid-call. Both mean the same thing
        # to a user: nobody heard you.
        raise HTTPException(status.HTTP_409_CONFLICT, f"{CANCEL_ESCAPE_HATCH} ({exc})") from exc
    except RpcError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"the job refused: {exc}") from exc

    log.info("cancel for %s acknowledged by the job (requested by %s)", run_id, requested_by)
    return {
        "run_id": run_id,
        "cancel_requested": True,
        "requested_by": requested_by,
        # Straight from the job. `accepted` and `already_cancelling` are its
        # words, not ours — the point of an ack is that it is the job speaking.
        "acknowledged": result,
    }


@router.get("/{run_id}/replay")
async def replay(
    run_id: str,
    hub: Hub,
    from_seq: int = Query(0, ge=0),
    to_seq: int | None = Query(None, ge=0),
) -> dict:
    """Fill a gap by asking the JOB to resend it.

    This is the design's keystone. The app holds no grant on the telemetry
    volume — deliberately, see uc_ddl/004_telemetry_volume.sql — so it cannot
    read the files even if it wanted to, and there is no warehouse to query.
    Asking the job is not one option among several; it is the only live
    backfill path there is.

    The job answers from its closed part files AND its in-flight buffer, so a
    client that just reconnected gets the newest records too — the ones a
    files-only implementation would silently omit.

    Client-triggered, never on a timer: a routine reconnect produces a gap of
    milliseconds, and demanding a click for a real one is honest.
    """
    try:
        result = await hub.job_sockets.call(
            run_id, Method.REPLAY, {"from_seq": from_seq, "to_seq": to_seq}
        )
    except ConnectionError as exc:
        # The run has finished, or was never observed. Its telemetry is on the
        # volume and reaches SQL through the ingestion job; it is simply not
        # available from here, live.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"no job attached to {run_id}; a finished run's history comes from SQL ({exc})",
        ) from exc
    except RpcError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"the job refused: {exc}") from exc

    messages = result.get("messages", []) if isinstance(result, dict) else []
    return {
        "run_id": run_id,
        "from_seq": from_seq,
        "to_seq": to_seq,
        "count": len(messages),
        "messages": messages,
    }
