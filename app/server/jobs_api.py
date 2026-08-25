"""Just enough of the Jobs API: launching a run, and asking how one ended.

Two uses, both event-driven and neither on a timer:

- ``run_now`` when someone triggers a model from the UI;
- ``get_run`` once at startup, to answer "did this run actually finish while
  we were down?" — the normal case when apps run ~8h/day and jobs do not.
"""

from __future__ import annotations

import logging
from typing import Any

from .oauth import bearer_headers

log = logging.getLogger(__name__)

__all__ = ["JobsApi", "JobsApiError", "JobsApiUnavailable", "TERMINAL_LIFE_CYCLE"]


class JobsApiError(RuntimeError):
    """The Jobs API refused something. Distinct from it being unreachable."""


class JobsApiUnavailable(JobsApiError):
    """No workspace configured — a degraded app, not a bad request."""


TERMINAL_LIFE_CYCLE = {"TERMINATED", "SKIPPED", "INTERNAL_ERROR"}

#: Databricks result_state -> our RunStatus.
_RESULT_STATE = {
    "SUCCESS": "SUCCEEDED",
    "FAILED": "FAILED",
    "TIMEDOUT": "FAILED",
    "CANCELED": "CANCELLED",
    "CANCELLED": "CANCELLED",
    # `termination_details.code` uses these where `result_state` says
    # CANCELED, and this is not a hypothetical spelling: it is what
    # `databricks jobs cancel-run` produces — the escape hatch
    # `app/server/routes/runs.py::CANCEL_ESCAPE_HATCH` tells users to reach for when
    # there is no live channel. Unmapped, it fell through the `.get` default
    # and reconciliation recorded a deliberate cancellation as a FAILED run.
    "USER_CANCELED": "CANCELLED",
    "USER_CANCELLED": "CANCELLED",
    "MAXIMUM_CONCURRENT_RUNS_REACHED": "FAILED",
    "EXCLUDED": "FAILED",
    "UPSTREAM_FAILED": "FAILED",
}


class JobsApi:
    def __init__(
        self,
        host: str | None,
        token: str | None,
        *,
        token_provider=None,
        client: Any = None,
    ) -> None:
        self.host = host.rstrip("/") if host else None
        self.token = token
        #: Awaited per request when set — see `oauth.bearer_headers`.
        self.token_provider = token_provider
        self._client = client
        self._owns_client = client is None

    @property
    def available(self) -> bool:
        return bool(self.host)

    async def _http(self) -> Any:
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def run_now(self, job_id: int, parameters: dict[str, str]) -> int:
        """Launch a job run. Returns Databricks' own run id.

        Parameters go through ``job_parameters``, which the bundle maps onto
        the environment variables ``job/config.py`` reads. Nothing is
        interpolated into a command line.
        """
        if not self.available:
            raise JobsApiUnavailable("no workspace host configured (DATABRICKS_HOST)")

        http = await self._http()
        headers = await bearer_headers(self.token, self.token_provider)
        resp = await http.post(
            f"{self.host}/api/2.2/jobs/run-now",
            json={"job_id": job_id, "job_parameters": parameters},
            headers=headers,
        )
        if resp.status_code >= 400:
            raise JobsApiError(
                f"run-now failed for job {job_id}: HTTP {resp.status_code} {resp.text[:400]}"
            )
        payload = resp.json()
        run_id = payload.get("run_id")
        if run_id is None:
            raise JobsApiError(f"run-now returned no run_id: {payload}")
        return int(run_id)

    async def get_run(self, job_run_id: str | int) -> dict[str, Any] | None:
        if not self.available:
            return None
        http = await self._http()
        headers = await bearer_headers(self.token, self.token_provider)
        try:
            resp = await http.get(
                f"{self.host}/api/2.2/jobs/runs/get",
                params={"run_id": str(job_run_id)},
                headers=headers,
            )
        except Exception:  # noqa: BLE001 - reconciliation is best-effort
            log.info("jobs api unreachable for run %s", job_run_id, exc_info=True)
            return None
        if resp.status_code >= 400:
            log.info("jobs api returned %s for run %s", resp.status_code, job_run_id)
            return None
        return resp.json()

    @staticmethod
    def terminal_status(run: dict[str, Any]) -> str | None:
        """The run's final status, or None if it is still going."""
        state = run.get("status") or run.get("state") or {}
        life_cycle = state.get("state") or state.get("life_cycle_state")
        if life_cycle not in TERMINAL_LIFE_CYCLE:
            return None
        termination = (state.get("termination_details") or {}).get("code")
        result = state.get("result_state") or termination
        return _RESULT_STATE.get(str(result).upper(), "FAILED")

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None
