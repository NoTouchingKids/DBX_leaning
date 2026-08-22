"""The job<->app wire protocol.

Two kinds of thing travel over that WebSocket: envelope messages (job -> app,
the actual run telemetry) and control frames (both directions — hello,
keepalive, and the one inbound command that exists, cancel).

They are tagged explicitly rather than sniffed apart by which keys happen to
be present. Implicit typing here would be a decoding bug waiting on a schema
change.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Union

import msgpack
from pydantic import BaseModel, ConfigDict, Field

from .envelope import Message, MessageAdapter, now_ms
from .codec import to_jsonable

__all__ = [
    "ControlKind",
    "ControlFrame",
    "Frame",
    "pack_frame",
    "unpack_frame",
    "hello",
    "cancel",
    "ping",
    "pong",
]


class ControlKind(str, Enum):
    #: job -> app, first frame: "this connection belongs to run X"
    HELLO = "hello"
    #: app -> job, acknowledges the hello (and reports the app's view)
    HELLO_ACK = "hello_ack"
    #: app -> job. The *only* inbound command. Sets the harness's
    #: threading.Event; there is no durable/warehouse-poll fallback for it.
    CANCEL = "cancel"
    #: App-level keepalive, both directions. Not a WS protocol ping — those
    #: can be answered by a proxy without ever reaching the handler, which
    #: makes them useless for telling "the ingress dropped this" from
    #: "nothing was sent for a while".
    PING = "ping"
    PONG = "pong"
    #: job -> app, clean shutdown: the run is over, expect nothing further.
    BYE = "bye"


class ControlFrame(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ControlKind
    run_id: str = Field(min_length=1)
    ts: int = Field(default_factory=now_ms)
    payload: dict[str, Any] = Field(default_factory=dict)


Frame = Union[Message, ControlFrame]

_MSG = "m"
_CTL = "c"


def pack_frame(frame: Frame) -> bytes:
    """msgpack, with an explicit one-byte discriminator for which kind it is."""
    if isinstance(frame, ControlFrame):
        body = frame.model_dump(mode="json")
        kind = _CTL
    else:
        body = to_jsonable(frame)
        kind = _MSG
    return msgpack.packb({"f": kind, "d": body}, use_bin_type=True)


def unpack_frame(raw: bytes) -> Frame:
    outer = msgpack.unpackb(raw, raw=False)
    if not isinstance(outer, dict) or "f" not in outer or "d" not in outer:
        raise ValueError("malformed frame: expected {'f': ..., 'd': ...}")
    kind = outer["f"]
    if kind == _CTL:
        return ControlFrame.model_validate(outer["d"])
    if kind == _MSG:
        return MessageAdapter.validate_python(outer["d"])
    raise ValueError(f"unknown frame discriminator {kind!r}")


def hello(run_id: str, *, job_run_id: str | None = None, next_seq: int = 0) -> ControlFrame:
    """First frame from a job. ``next_seq`` tells the app where this
    connection picks up — a job that has been running unobserved for an hour
    attaches at seq 4,000, not 0."""
    return ControlFrame(
        kind=ControlKind.HELLO,
        run_id=run_id,
        payload={"job_run_id": job_run_id, "next_seq": next_seq},
    )


def cancel(run_id: str, *, requested_by: str | None = None) -> ControlFrame:
    return ControlFrame(
        kind=ControlKind.CANCEL, run_id=run_id, payload={"requested_by": requested_by}
    )


def ping(run_id: str) -> ControlFrame:
    return ControlFrame(kind=ControlKind.PING, run_id=run_id)


def pong(run_id: str) -> ControlFrame:
    return ControlFrame(kind=ControlKind.PONG, run_id=run_id)
