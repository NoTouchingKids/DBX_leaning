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
   instead of a poll loop — and a poll loop only when that is not enough.

The warehouse is usually ASLEEP
-------------------------------

Auto-stop is 10 minutes and this is a low-traffic internal tool, so the first
query after any quiet period arrives at a stopped warehouse and waits for it
to start. That is the normal case here, not an edge one.

This module used to send ``on_wait_timeout: CANCEL``, which tells Databricks
to cancel the statement if it has not finished within ``wait_timeout`` (50s at
most, 30 by default). A cold 2X-Small takes longer than that to come up, so
the app cancelled its own statement and reported::

    server.sql.StatementError: statement CANCELED: no detail

— "no detail" because a statement cancelled by that flag carries no error
message. Every read 500'd and every trigger 503'd for the ~90 seconds the
warehouse took to start, then recovered on its own, which made it look
intermittent rather than deterministic.

So the wait is now ``CONTINUE`` and the client polls to a deadline. Retrying
the whole statement instead would be worse: each attempt re-queues behind the
same starting warehouse and throws away the one already queued. Giving up
cancels explicitly, so nothing is left running for a caller that has gone.

None of this costs extra warehouse time — the cost is the *start*, which is
already happening. What it changes is whether the app fails while waiting.

The structural fix is elsewhere: ``run_status`` belongs in Lakebase (see
``store.py``), and with that configured neither the run list nor the
concurrency check touches the warehouse at all. This path then serves only
backfill, where a cold start is worth waiting for.
"""

from __future__ import annotations

import asyncio
import builtins
import logging
import time
from dataclasses import dataclass
from typing import Any

from .oauth import bearer_headers

log = logging.getLogger(__name__)

__all__ = ["Param", "P", "SqlClient", "SqlUnavailable", "StatementError"]


class SqlUnavailable(RuntimeError):
    """No warehouse configured. A degraded read path, not a crash."""


class StatementError(RuntimeError):
    pass


#: Statement states that will not change again.
TERMINAL_STATES = frozenset({"SUCCEEDED", "FAILED", "CANCELED", "CLOSED"})

#: How long to keep asking. Bounded so a request cannot hang forever, and
#: generous because the thing being waited on is a warehouse cold start.
DEFAULT_STATEMENT_DEADLINE_S = 180.0

#: Poll spacing while a statement is PENDING or RUNNING. Backs off so a long
#: wait is a handful of requests rather than hundreds.
_POLL_FIRST_S = 1.0
_POLL_MAX_S = 5.0


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
        token_provider=None,
        wait_timeout_s: int = 30,
        timeout_s: float = 60.0,
        statement_deadline_s: float = DEFAULT_STATEMENT_DEADLINE_S,
        client: Any = None,
    ) -> None:
        self.host = host.rstrip("/") if host else None
        self.warehouse_id = warehouse_id
        self.token = token
        #: Awaited per request when set — see `oauth.bearer_headers`.
        self.token_provider = token_provider
        self.wait_timeout_s = max(5, min(50, wait_timeout_s))
        self.timeout_s = timeout_s
        #: Total time a statement may take, first wait plus polling. Never
        #: shorter than the first wait, which would make polling unreachable.
        self.statement_deadline_s = max(float(statement_deadline_s), self.wait_timeout_s)
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
            raise SqlUnavailable("no SQL warehouse configured (DATABRICKS_HOST / DBX_WAREHOUSE_ID)")
        body: dict[str, Any] = {
            "statement": sql,
            "warehouse_id": self.warehouse_id,
            # INLINE supports JSON_ARRAY only and aborts past 25 MiB — fine
            # for backfill pages, which is all this path is for.
            "disposition": "INLINE",
            "format": "JSON_ARRAY",
            "wait_timeout": f"{self.wait_timeout_s}s",
            # CONTINUE, not CANCEL. See the module docstring: CANCEL made the
            # app cancel its own statement whenever the warehouse was starting,
            # which is most first requests of the day.
            "on_wait_timeout": "CONTINUE",
        }
        if params:
            body["parameters"] = [p.as_api() for p in params]

        http = await self._http()
        headers = await bearer_headers(self.token, self.token_provider)
        resp = await http.post(f"{self.host}/api/2.0/sql/statements", json=body, headers=headers)
        self.statements += 1
        if resp.status_code >= 400:
            raise StatementError(f"statement failed: HTTP {resp.status_code} {resp.text[:400]}")

        payload = await self._settle(resp.json(), http, headers)
        state = _state(payload)
        if state != "SUCCEEDED":
            raise StatementError(_failure(payload))
        return _rows(payload)

    async def _settle(self, payload: dict[str, Any], http: Any, headers: dict) -> dict[str, Any]:
        """Poll until the statement reaches a state that will not change.

        The first response usually IS terminal — `wait_timeout` means a warm
        warehouse answers in one round trip, which is the whole point of using
        it. This only does work when the warehouse is starting.
        """
        started = time.monotonic()
        delay = _POLL_FIRST_S

        while _state(payload) not in TERMINAL_STATES:
            statement_id = payload.get("statement_id")
            if not statement_id:
                # Non-terminal with nothing to poll: there is no way forward,
                # and looping would spin.
                raise StatementError(f"statement {_state(payload)} with no statement_id to poll")

            waited = time.monotonic() - started
            if waited >= self.statement_deadline_s:
                await self._cancel(statement_id, http, headers)
                raise StatementError(
                    f"statement {_state(payload)} after {waited:.0f}s "
                    f"(deadline {self.statement_deadline_s:.0f}s) — the warehouse may still be "
                    f"starting; cancelled {statement_id}"
                )

            await asyncio.sleep(min(delay, self.statement_deadline_s - waited))
            delay = min(delay * 2, _POLL_MAX_S)

            resp = await http.get(
                f"{self.host}/api/2.0/sql/statements/{statement_id}", headers=headers
            )
            if resp.status_code >= 400:
                raise StatementError(
                    f"polling {statement_id} failed: HTTP {resp.status_code} {resp.text[:400]}"
                )
            payload = resp.json()

        return payload

    async def _cancel(self, statement_id: str, http: Any, headers: dict) -> None:
        """Best-effort. Nothing useful follows a failed cancel, but leaving a
        statement running for a caller that has given up is worse than trying."""
        try:
            await http.post(
                f"{self.host}/api/2.0/sql/statements/{statement_id}/cancel", headers=headers
            )
        except Exception:  # noqa: BLE001
            log.info("could not cancel statement %s", statement_id, exc_info=True)

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None


def _state(payload: dict[str, Any]) -> str:
    return (payload.get("status") or {}).get("state") or "UNKNOWN"


def _failure(payload: dict[str, Any]) -> str:
    """Say something useful even when the API says nothing.

    A statement cancelled by `on_wait_timeout` carries no error message at
    all, which produced "statement CANCELED: no detail" — true, and no help to
    anyone. Whatever is here, the statement id is, so the workspace's query
    history can be pointed at.
    """
    status = payload.get("status") or {}
    error = status.get("error") or {}
    detail = error.get("message") or error.get("error_code")
    if not detail:
        detail = "no message from the API"
    return f"statement {_state(payload)}: {detail} (statement_id={payload.get('statement_id')})"


def _rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    manifest = payload.get("manifest") or {}
    columns = [c["name"] for c in (manifest.get("schema") or {}).get("columns", [])]
    data = (payload.get("result") or {}).get("data_array") or []
    return [dict(zip(columns, row, strict=False)) for row in data]
