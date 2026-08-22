"""Just enough of the Jobs API for startup reconciliation.

Used once, at startup, to answer "did this run actually finish while we were
down?" — the normal case when apps run ~8h/day and jobs do not.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

__all__ = ["JobsApi", "TERMINAL_LIFE_CYCLE"]

TERMINAL_LIFE_CYCLE = {"TERMINATED", "SKIPPED", "INTERNAL_ERROR"}

#: Databricks result_state -> our RunStatus.
_RESULT_STATE = {
    "SUCCESS": "SUCCEEDED",
    "FAILED": "FAILED",
    "TIMEDOUT": "FAILED",
    "CANCELED": "CANCELLED",
    "CANCELLED": "CANCELLED",
    "MAXIMUM_CONCURRENT_RUNS_REACHED": "FAILED",
    "EXCLUDED": "FAILED",
    "UPSTREAM_FAILED": "FAILED",
}


class JobsApi:
    def __init__(self, host: str | None, token: str | None, *, client: Any = None) -> None:
        self.host = host.rstrip("/") if host else None
        self.token = token
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

    async def get_run(self, job_run_id: str | int) -> dict[str, Any] | None:
        if not self.available:
            return None
        http = await self._http()
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
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
