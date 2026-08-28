"""Reporting the run's status to Lakebase, over the Database REST API.

**Why the job writes this at all.** `run_status` is the record of truth for
"what is this run doing", and until now only the app maintained it — from
status messages arriving over the socket. That made a fact about the run
depend on the observer being up and the socket being healthy. It is not:
the job knows its own status, so the job reports it.

The consequence worth having: a run whose socket never attaches — the app was
down, the proxy refused the handshake, the deploy has no `DBX_APP_URL` — still
keeps `run_status` current, instead of sitting at whatever the app last saw
until startup reconciliation notices.

**Who writes what.** The app still owns the slot claim: the count-and-claim
transaction that makes the 5-concurrent-task ceiling real, and the row's
creation at trigger time. The job owns the status transitions on that row.
One writer per concern, and the UPSERT's `updated_ts` guard means an
out-of-order write cannot move the row backwards even so.

**REST rather than psycopg**, because a Postgres driver in this process
would be paid for by all eleven model environments (`CLAUDE.md`: one job per
model, each with its own dependency list). `httpx` is already here for the
OAuth exchange in `job/auth.py`.

**Nothing here is load-bearing.** Unconfigured, unreachable, refused — all of
them log and carry on. The durable record of a status transition is the
`run_events` row the harness writes for every status message (`shared/tables.py`
routes it there); this is the live, point-lookup copy that the app and a
browser read. Losing it costs freshness, not the record.
"""

from __future__ import annotations

import logging
from typing import Any

from .auth import AppCredential
from .record import RunRecord

log = logging.getLogger(__name__)

__all__ = ["LakebaseStatus", "UPSERT_SQL"]

#: Upsert rather than update: a job triggered outside the app (a schedule, a
#: manual `run-now`) has no row yet, and refusing to record its status
#: because nobody claimed a slot for it would lose exactly the runs nobody is
#: watching.
#:
#: The `WHERE` guard is what makes two writers safe. The app writes this row
#: too, and Databricks can deliver a retry out of order; without the guard a
#: late RUNNING would overwrite a SUCCEEDED that already landed.
UPSERT_SQL = """
INSERT INTO {schema}.run_status
    (run_id, job_run_id, model, status, detail, started_ts, updated_ts)
VALUES ($1, $2, $3, $4, $5, $6, $7)
ON CONFLICT (run_id) DO UPDATE SET
    status      = EXCLUDED.status,
    detail      = EXCLUDED.detail,
    updated_ts  = EXCLUDED.updated_ts,
    job_run_id  = COALESCE(EXCLUDED.job_run_id, {schema}.run_status.job_run_id),
    model       = COALESCE({schema}.run_status.model, EXCLUDED.model),
    started_ts  = COALESCE({schema}.run_status.started_ts, EXCLUDED.started_ts)
WHERE {schema}.run_status.updated_ts <= EXCLUDED.updated_ts
""".strip()


class LakebaseStatus:
    """One status row, kept current over HTTP.

    ``endpoint`` is the full URL of the Database REST API's statement
    execution path for the target instance. It is configuration rather than
    something derived here, because the path is a property of how the
    instance was provisioned and this process should not have to guess it —
    see ``DBX_LAKEBASE_REST_URL`` in ``job/config.py``.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        schema: str = "dbx_leaning",
        credential: AppCredential | None = None,
        timeout_s: float = 10.0,
        client: Any = None,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.schema = schema
        self.credential = credential
        self.timeout_s = timeout_s
        self._client = client
        self._owns_client = client is None
        self.writes = 0
        self.failures = 0
        self.last_error: str | None = None

    @property
    def available(self) -> bool:
        return bool(self.endpoint)

    async def _http(self) -> Any:
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(timeout=self.timeout_s)
        return self._client

    def _body(self, record: RunRecord, summary: dict[str, Any]) -> dict[str, Any]:
        """The request the REST API is handed.

        Isolated in one method on purpose: it is the only part of this file
        that depends on the Database REST API's exact envelope, so pointing
        it at a different shape is a change here and nowhere else.
        """
        return {
            "statement": UPSERT_SQL.format(schema=self.schema),
            "parameters": [
                summary["run_id"],
                summary["job_run_id"],
                summary["model"],
                summary["status"],
                summary["detail"],
                summary["started_ts"],
                summary["updated_ts"],
            ],
        }

    async def report(self, record: RunRecord, *, requested_by: str | None = None) -> bool:
        """Push the record's current status. True when it landed.

        Never raises: a status report that cannot be delivered is a live-path
        problem, and the live path is never load-bearing here.
        """
        if not self.available:
            return False
        summary = record.summary(requested_by=requested_by)
        headers: dict[str, str] = {}
        if self.credential is not None:
            token = await self.credential.token()
            if token:
                headers["Authorization"] = f"Bearer {token}"

        try:
            client = await self._http()
            response = await client.post(
                self.endpoint, json=self._body(record, summary), headers=headers
            )
            ok = 200 <= response.status_code < 300
            if not ok:
                self.last_error = f"HTTP {response.status_code} {response.text[:200]}"
        except Exception as exc:  # noqa: BLE001 - every failure mode is "log and carry on"
            ok = False
            self.last_error = f"{type(exc).__name__}: {exc}"

        if ok:
            self.writes += 1
            return True
        self.failures += 1
        log.info(
            "could not report %s -> %s to Lakebase (%s); run_events and the "
            "end-of-run Delta write still carry it",
            summary["run_id"],
            summary["status"],
            self.last_error,
        )
        return False

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None and self._owns_client:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001
                log.debug("lakebase client close failed", exc_info=True)
