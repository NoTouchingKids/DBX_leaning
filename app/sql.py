"""Read path: Unity Catalog via the Statement Execution API.

Reads only. The write path deliberately never touches the warehouse — cost is
driven by *uptime*, not statement count, so anything on a short interval keeps
it awake and costs real money regardless of how small the query is.

Three rules this module enforces rather than trusts:

1. **Bound parameters, always, with an explicit type.** An untyped parameter
   is compared as a string server-side, which is how ``"2" > "12"`` broke
   cursor logic twice in v1. ``P.int(...)`` exists so a caller cannot forget.
2. **No ORM, no query builder.** Plain SQL text.
3. **The API's own ``wait_timeout``**, so a fast statement is one round trip
   instead of a poll loop.
"""

from __future__ import annotations

import builtins
import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

__all__ = ["Param", "P", "SqlClient", "SqlUnavailable", "StatementError"]


class SqlUnavailable(RuntimeError):
    """No warehouse configured. A degraded read path, not a crash."""


class StatementError(RuntimeError):
    pass


@dataclass(frozen=True)
class Param:
    name: str
    value: Any
    type: str

    def as_api(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": None if self.value is None else str(self.value),
            "type": self.type,
        }


class P:
    """Typed parameter constructors. Use these, not raw dicts.

    Annotations say ``builtins.str`` because ``P.str`` shadows the builtin
    inside this class body — it resolves correctly at runtime, but spelling it
    out costs nothing and removes the ambiguity for a reader.
    """

    @staticmethod
    def str(name: builtins.str, value: Any) -> Param:
        # Coerce here, not at send time: a STRING parameter holding an int is
        # a declared type and a stored value disagreeing, which is the same
        # class of bug the typed parameters exist to prevent.
        return Param(name, None if value is None else builtins.str(value), "STRING")

    @staticmethod
    def int(name: builtins.str, value: Any) -> Param:
        return Param(name, int(value) if value is not None else None, "INT")

    @staticmethod
    def bigint(name: builtins.str, value: Any) -> Param:
        return Param(name, int(value) if value is not None else None, "BIGINT")

    @staticmethod
    def double(name: builtins.str, value: Any) -> Param:
        return Param(name, float(value) if value is not None else None, "DOUBLE")

    @staticmethod
    def bool(name: builtins.str, value: Any) -> Param:
        return Param(name, "true" if value else "false", "BOOLEAN")


class SqlClient:
    """Async client over the Statement Execution API.

    ``client`` is injectable so every route above this can be tested without a
    warehouse — which also means the app's read path degrades to a clean 503
    rather than an AttributeError when no warehouse is configured.
    """

    def __init__(
        self,
        host: str | None,
        warehouse_id: str | None,
        token: str | None,
        *,
        wait_timeout_s: int = 30,
        timeout_s: float = 60.0,
        client: Any = None,
    ) -> None:
        self.host = host.rstrip("/") if host else None
        self.warehouse_id = warehouse_id
        self.token = token
        self.wait_timeout_s = max(5, min(50, wait_timeout_s))
        self.timeout_s = timeout_s
        self._client = client
        self._owns_client = client is None
        self.statements = 0

    @property
    def available(self) -> bool:
        return bool(self.host and self.warehouse_id)

    async def _http(self) -> Any:
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(timeout=self.timeout_s)
        return self._client

    async def query(self, sql: str, params: list[Param] | None = None) -> list[dict[str, Any]]:
        if not self.available:
            raise SqlUnavailable(
                "no SQL warehouse configured (DATABRICKS_HOST / DBX_WAREHOUSE_ID)"
            )
        body: dict[str, Any] = {
            "statement": sql,
            "warehouse_id": self.warehouse_id,
            # INLINE supports JSON_ARRAY only and aborts past 25 MiB — fine
            # for backfill pages, which is all this path is for.
            "disposition": "INLINE",
            "format": "JSON_ARRAY",
            "wait_timeout": f"{self.wait_timeout_s}s",
            "on_wait_timeout": "CANCEL",
        }
        if params:
            body["parameters"] = [p.as_api() for p in params]

        http = await self._http()
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        resp = await http.post(
            f"{self.host}/api/2.0/sql/statements", json=body, headers=headers
        )
        self.statements += 1
        if resp.status_code >= 400:
            raise StatementError(f"statement failed: HTTP {resp.status_code} {resp.text[:400]}")

        payload = resp.json()
        state = (payload.get("status") or {}).get("state")
        if state != "SUCCEEDED":
            error = (payload.get("status") or {}).get("error") or {}
            raise StatementError(f"statement {state}: {error.get('message', 'no detail')}")
        return _rows(payload)

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None


def _rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    manifest = payload.get("manifest") or {}
    columns = [c["name"] for c in (manifest.get("schema") or {}).get("columns", [])]
    data = (payload.get("result") or {}).get("data_array") or []
    return [dict(zip(columns, row, strict=False)) for row in data]
