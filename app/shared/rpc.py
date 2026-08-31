"""The job<->app protocol: JSON-RPC 2.0 over one WebSocket.

**This is the surface that gates the app rewrite.** `docs/v4-rewrite-plan.md`
defers moving the app to another language until "the RPC method surface has
stopped changing", because rebuilding against a moving contract in a language
you are still learning is how a rewrite stalls. So the method set below is
deliberately small, and adding to it is a decision rather than a convenience.

Why RPC at all, when v3's one-way frames worked: three things it does that
they could not.

1. **Cancel is acknowledged.** v3 set a flag and replied nothing, so the app
   could not tell "delivered" from "lost".
2. **`replay` exists.** The app holds no grant on the telemetry volume, by
   design, so asking the job for a gap is the *only* live backfill path there
   is — and a request without a response cannot carry records back.
3. **Errors have one shape** instead of a per-call convention.

Why JSON-RPC specifically rather than gRPC: gRPC needs HTTP/2 end-to-end with
trailers through the Databricks Apps ingress, which is not what the spikes
cleared. WebSocket `Upgrade` is. Betting a rewrite on an unverified ingress
assumption is what produced v1 and v2. JSON-RPC rides the `Upgrade` already
proven to work, and the framing is a dozen lines.

Encoding is JSON both directions — see the plan. One codec, readable in
devtools, and `replay` parses the same bytes an operator reads out of a
telemetry part file.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = [
    "JSONRPC_VERSION",
    "Method",
    "ErrorCode",
    "request",
    "notification",
    "success",
    "failure",
    "parse",
    "Request",
    "Response",
    "RpcError",
]

JSONRPC_VERSION = "2.0"


class Method:
    """Every method either side may call. Six, and each earns its place.

    Direction is a convention, not something the framing enforces — but it is
    a real part of the contract, so it is written down here rather than
    discovered by reading both ends.
    """

    # --- job -> app -------------------------------------------------------

    #: NOTIFICATION. A batch of envelope messages. The bulk of all traffic.
    #: Deliberately not a request: acknowledging every telemetry batch would
    #: double the frames to tell us something the durable path already
    #: guarantees. Telemetry is best-effort *on this channel*; the volume is
    #: what makes it durable.
    TELEMETRY = "telemetry"

    #: REQUEST. First frame of a connection: which run this is, and where it
    #: picks up. A job that has run unobserved for an hour attaches at seq
    #: 4,000, not 0 — so the app learns immediately that it has a gap rather
    #: than inferring one from a jump.
    HELLO = "hello"

    #: NOTIFICATION. Clean shutdown: the run is over, expect nothing further.
    #: Distinct from a dropped socket, which means "try again".
    BYE = "bye"

    # --- app -> job -------------------------------------------------------

    #: REQUEST. The only command that mutates a run. Answered with whether the
    #: job accepted it — the acknowledgement v3 could not give.
    CANCEL = "cancel"

    #: REQUEST. Resend `[from_seq, to_seq]` from the job's own telemetry.
    #: The keystone: the app cannot read the telemetry volume (it holds no
    #: grant, deliberately), so this is the only live backfill there is.
    REPLAY = "replay"

    # --- either direction -------------------------------------------------

    #: REQUEST. App-level keepalive. Deliberately NOT a WebSocket protocol
    #: ping: those can be answered by a proxy without ever reaching the
    #: handler, which makes them useless for telling "the ingress dropped
    #: this" from "nothing was sent for a while".
    PING = "ping"


class ErrorCode:
    """JSON-RPC reserves -32768..-32000; ours live outside that."""

    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603

    #: The run named is not the one this connection belongs to, or is unknown.
    UNKNOWN_RUN = -31001
    #: `replay` for records the job no longer has. Not an error the caller can
    #: retry away: see the note on `replay` in `job/telemetry.py`.
    RECORDS_GONE = -31002


class RpcError(Exception):
    """An error carried as a JSON-RPC error object rather than a stack trace."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            out["data"] = self.data
        return out


class Request:
    """A method call. `id is None` means a notification — no reply expected,
    and none may be sent."""

    __slots__ = ("id", "method", "params")

    def __init__(self, method: str, params: dict[str, Any], id: int | str | None) -> None:
        self.method = method
        self.params = params
        self.id = id

    @property
    def is_notification(self) -> bool:
        return self.id is None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        kind = "notify" if self.is_notification else f"request id={self.id}"
        return f"<{kind} {self.method} params={sorted(self.params)}>"


class Response:
    """A reply to a request. Exactly one of `result` / `error` is set."""

    __slots__ = ("id", "result", "error")

    def __init__(
        self, id: int | str | None, result: Any = None, error: dict[str, Any] | None = None
    ) -> None:
        self.id = id
        self.result = result
        self.error = error

    @property
    def ok(self) -> bool:
        return self.error is None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<response id={self.id} {'ok' if self.ok else self.error}>"


def request(method: str, params: dict[str, Any] | None = None, *, id: int | str) -> bytes:
    return _encode({"jsonrpc": JSONRPC_VERSION, "id": id, "method": method, "params": params or {}})


def notification(method: str, params: dict[str, Any] | None = None) -> bytes:
    """No `id`, which is what makes it a notification — a reply would be a
    protocol error, not merely unnecessary."""
    return _encode({"jsonrpc": JSONRPC_VERSION, "method": method, "params": params or {}})


def success(id: int | str | None, result: Any) -> bytes:
    return _encode({"jsonrpc": JSONRPC_VERSION, "id": id, "result": result})


def failure(id: int | str | None, error: RpcError) -> bytes:
    return _encode({"jsonrpc": JSONRPC_VERSION, "id": id, "error": error.as_dict()})


def parse(raw: str | bytes) -> Request | Response:
    """Decode one frame. Raises `RpcError` on anything malformed.

    Deliberately strict about the discriminators — a frame is a request if it
    has `method`, a response if it has `result` or `error`, and anything else
    is rejected rather than guessed at. Sniffing frames apart by which keys
    happen to be present is a decoding bug waiting on a schema change, which
    is the same reason v3's frames carried an explicit tag.
    """
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise RpcError(ErrorCode.PARSE_ERROR, f"not JSON: {exc}") from None

    if not isinstance(obj, dict):
        raise RpcError(ErrorCode.INVALID_REQUEST, "frame is not an object")
    if obj.get("jsonrpc") != JSONRPC_VERSION:
        raise RpcError(
            ErrorCode.INVALID_REQUEST,
            f"jsonrpc must be {JSONRPC_VERSION!r}, got {obj.get('jsonrpc')!r}",
        )

    if "method" in obj:
        method = obj["method"]
        if not isinstance(method, str) or not method:
            raise RpcError(ErrorCode.INVALID_REQUEST, "method must be a non-empty string")
        params = obj.get("params") or {}
        if not isinstance(params, dict):
            # Positional params are legal JSON-RPC and deliberately unsupported:
            # one shape means call sites cannot disagree about argument order.
            raise RpcError(ErrorCode.INVALID_PARAMS, "params must be an object, not an array")
        return Request(method, params, obj.get("id"))

    if "result" in obj or "error" in obj:
        return Response(obj.get("id"), obj.get("result"), obj.get("error"))

    raise RpcError(ErrorCode.INVALID_REQUEST, "frame is neither a request nor a response")


def _encode(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode()
