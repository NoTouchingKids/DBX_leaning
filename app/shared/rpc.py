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

**The shape is JSON-RPC 2.0, exactly** — `jsonrpc: "2.0"` on every frame; a
request carries `id`, `method`, `params`; a notification is a request with no
`id`; a response carries `id` and exactly one of `result` or
`error: {code, message, data?}`. Two deliberate subsets of it: `params` is
always an object (never positional), and batches (a JSON array of frames) are
not accepted. Every frame is a WebSocket TEXT frame — see `_encode`.

**Versioning** follows LSP's `initialize`: the first frame on a connection is
`hello`, whose params carry the job's `protocol_version` and a `capabilities`
object, and whose result carries the app's. The compatibility rule is
`is_compatible` below, and it is the whole rule.
"""

from __future__ import annotations

import json
import re
from typing import Any

__all__ = [
    "JSONRPC_VERSION",
    "PROTOCOL_VERSION",
    "parse_protocol_version",
    "is_compatible",
    "check_hello_version",
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

#: This side's version of the job<->app protocol, "MAJOR.MINOR". Sent by the
#: job in `hello`'s params and by the app in `hello`'s result.
#:
#: Bump MINOR for an addition an older app can safely ignore — a new optional
#: envelope field, a new message `type`, a new method the other side need not
#: call. Bump MAJOR (and reset MINOR) for anything an older app would misread.
#: The rule that turns this into a promise is `is_compatible`.
PROTOCOL_VERSION = "1.0"

_VERSION_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)$")


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

    #: REQUEST. First frame of a connection: which run this is, where it picks
    #: up, and which protocol it speaks. A job that has run unobserved for an
    #: hour attaches at seq 4,000, not 0 — so the app learns immediately that
    #: it has a gap rather than inferring one from a jump.
    #:
    #: params: `run_id`, `next_seq`, `protocol_version` ("MAJOR.MINOR",
    #: REQUIRED), `capabilities` (object: the methods this side answers).
    #: result: `observed`, `run_id`, `protocol_version`, `capabilities` — the
    #: app's. Modelled on LSP's `initialize`. The app processes nothing else
    #: on a connection until a `hello` has been accepted, and a rejected one
    #: is answered with an error and the socket closed.
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
    #: `hello` named a protocol version this app does not accept under
    #: `is_compatible`. Final for that job: retrying cannot change the answer.
    INCOMPATIBLE_VERSION = -31003


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


def parse_protocol_version(value: Any) -> tuple[int, int]:
    """`"MAJOR.MINOR"` -> `(major, minor)`. Raises `ValueError` otherwise.

    Strict on purpose: a string, two non-negative integers, no leading zeros,
    no patch component, no `v` prefix. A float `1.0` is refused too — JSON
    cannot tell `1.10` from `1.1` once it is a number.
    """
    if not isinstance(value, str):
        raise ValueError(f"protocol_version must be a string 'MAJOR.MINOR', got {value!r}")
    m = _VERSION_RE.match(value)
    if m is None:
        raise ValueError(f"protocol_version must look like 'MAJOR.MINOR', got {value!r}")
    return int(m.group(1)), int(m.group(2))


def is_compatible(job_version: str, app_version: str = PROTOCOL_VERSION) -> bool:
    """THE compatibility rule, and the only place it is written in code.

    The app accepts a job whose MAJOR equals the app's and whose MINOR is less
    than or equal to the app's. So an app at 1.3 accepts jobs at 1.0-1.3, and
    refuses 1.4 (it may send something the app has never heard of in a way
    that matters) and 2.x / 0.x (a different protocol). An app upgraded ahead
    of its jobs keeps observing them; a job upgraded ahead of its app runs
    unobserved until the app catches up — which costs nothing durable.

    Raises `ValueError` if either side is not a well-formed version.
    """
    j_major, j_minor = parse_protocol_version(job_version)
    a_major, a_minor = parse_protocol_version(app_version)
    return j_major == a_major and j_minor <= a_minor


def check_hello_version(params: dict[str, Any], app_version: str = PROTOCOL_VERSION) -> str:
    """Validate `hello`'s `protocol_version`; return it, or raise `RpcError`.

    Missing or malformed is INVALID_PARAMS — the field is mandatory, and a
    version the app cannot even read is not one it can judge. Well-formed but
    outside the rule is INCOMPATIBLE_VERSION. Both carry the app's version and
    the rule in `data`, so the job can log exactly why it is unobserved.
    """
    data = {
        "app_protocol_version": app_version,
        "rule": "job MAJOR == app MAJOR and job MINOR <= app MINOR",
    }
    if "protocol_version" not in params:
        raise RpcError(
            ErrorCode.INVALID_PARAMS,
            "hello must carry protocol_version ('MAJOR.MINOR')",
            data=data,
        )
    job_version = params["protocol_version"]
    try:
        ok = is_compatible(job_version, app_version)
    except ValueError as exc:
        raise RpcError(ErrorCode.INVALID_PARAMS, str(exc), data=data) from None
    if not ok:
        raise RpcError(
            ErrorCode.INCOMPATIBLE_VERSION,
            f"job protocol {job_version} is not compatible with app protocol {app_version}",
            data={**data, "job_protocol_version": job_version},
        )
    return job_version


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


def request(method: str, params: dict[str, Any] | None = None, *, id: int | str) -> str:
    return _encode({"jsonrpc": JSONRPC_VERSION, "id": id, "method": method, "params": params or {}})


def notification(method: str, params: dict[str, Any] | None = None) -> str:
    """No `id`, which is what makes it a notification — a reply would be a
    protocol error, not merely unnecessary."""
    return _encode({"jsonrpc": JSONRPC_VERSION, "method": method, "params": params or {}})


def success(id: int | str | None, result: Any) -> str:
    return _encode({"jsonrpc": JSONRPC_VERSION, "id": id, "result": result})


def failure(id: int | str | None, error: RpcError) -> str:
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

    frame_id = obj.get("id")
    if frame_id is not None and (isinstance(frame_id, bool) or not isinstance(frame_id, int | str)):
        raise RpcError(ErrorCode.INVALID_REQUEST, "id must be a string, an integer, or absent")

    if "method" in obj:
        method = obj["method"]
        if not isinstance(method, str) or not method:
            raise RpcError(ErrorCode.INVALID_REQUEST, "method must be a non-empty string")
        params = obj.get("params") or {}
        if not isinstance(params, dict):
            # Positional params are legal JSON-RPC and deliberately unsupported:
            # one shape means call sites cannot disagree about argument order.
            raise RpcError(ErrorCode.INVALID_PARAMS, "params must be an object, not an array")
        return Request(method, params, frame_id)

    if "result" in obj and "error" in obj:
        raise RpcError(ErrorCode.INVALID_REQUEST, "a response carries result or error, not both")
    if "error" in obj:
        error = obj["error"]
        if (
            not isinstance(error, dict)
            or isinstance(error.get("code"), bool)
            or not isinstance(error.get("code"), int)
            or not isinstance(error.get("message"), str)
        ):
            raise RpcError(
                ErrorCode.INVALID_REQUEST, "error must be an object with integer code and message"
            )
        return Response(frame_id, None, error)
    if "result" in obj:
        return Response(frame_id, obj["result"])

    raise RpcError(ErrorCode.INVALID_REQUEST, "frame is neither a request nor a response")


def _encode(payload: dict[str, Any]) -> str:
    """TEXT, not bytes, and the type is the safeguard.

    Every frame builder returns `str` so that a caller doing the obvious thing
    — `ws.send(notification(...))` — sends a WebSocket TEXT frame. Returning
    `bytes` made that same obvious call send a BINARY frame, which the app
    then failed to read:

        raw = await websocket.receive_text()
        KeyError: 'text'

    Starlette's `receive_text()` wants `message["text"]`; a binary frame
    carries `message["bytes"]` instead. The app had `.decode()` at all five of
    its own send sites and was correct; `job/ws.py` had six sends without one
    and was not. Nothing caught it because each side was tested against a
    stand-in more lenient than the real counterpart — `websockets.sync.server`
    accepts either kind, and the app's tests built their own text frames
    rather than using the job's client.

    `parse()` still accepts `str | bytes`: strict in what we send, liberal in
    what we accept.
    """
    return json.dumps(payload, separators=(",", ":"))
