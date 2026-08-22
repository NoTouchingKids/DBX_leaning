"""The message envelope.

One shape, one ``type`` discriminator, for everything a run produces. The
same object travels over the WebSocket, over HTTP push, and into Delta —
see ``docs/message-envelope-spec.md``, which this module implements.

Nothing here knows about a transport. Encoding lives in ``shared.codec``;
Delta's flat-row projection lives in ``shared.tables``.
"""

from __future__ import annotations

import math
import time
from enum import Enum
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

__all__ = [
    "MessageType",
    "LogLevel",
    "RunStatus",
    "TERMINAL_STATUSES",
    "LogMessage",
    "ProgressMessage",
    "StatusMessage",
    "ResultMessage",
    "Message",
    "MessageAdapter",
    "make_message",
    "now_ms",
    "sanitize_metric",
    "PREVIEW_MAX_POINTS",
]

#: Upper bound on ``result.preview`` length. The preview exists to render a
#: chart instantly; the full result set lives in the model's own UC table and
#: is read from there (spec: "~500-1000 points").
PREVIEW_MAX_POINTS = 1000


def now_ms() -> int:
    """Epoch milliseconds. Not a formatted timestamp, deliberately.

    Milliseconds, not seconds: solver log lines can be sub-millisecond apart
    and epoch seconds would collide.
    """
    return time.time_ns() // 1_000_000


def sanitize_metric(value: float | None) -> float | None:
    """NaN/inf -> None. Anything non-finite poisons a chart axis downstream.

    This is a floor, not the whole story: Gurobi's pre-incumbent sentinel is
    ``±1e100``, which is *finite* and therefore invisible here. That one is
    handled where it is produced — see ``models/gurobi_scheduling``.
    """
    if value is None:
        return None
    value = float(value)
    return None if not math.isfinite(value) else value


class MessageType(str, Enum):
    LOG = "log"
    PROGRESS = "progress"
    STATUS = "status"
    RESULT = "result"


class LogLevel(str, Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class RunStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    INFEASIBLE = "INFEASIBLE"


#: A run in one of these is finished; nothing further will arrive for it.
TERMINAL_STATUSES = frozenset(
    {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.INFEASIBLE}
)


class _Common(BaseModel):
    """Fields every message carries.

    Frozen: a message is a record of something that already happened. Nothing
    downstream — relay, buffer, codec — has any business editing one in place.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)

    run_id: str = Field(min_length=1)
    #: Assigned by the job, one monotonic counter per run shared across all
    #: message types. Never a UC identity column: only a value known *before*
    #: the durable write can reconcile live records against backfilled ones.
    seq: int = Field(ge=0)
    ts: int = Field(ge=0, description="epoch milliseconds")


class LogMessage(_Common):
    """Best-effort. May be dropped on the live path; never on the durable one."""

    type: Literal[MessageType.LOG] = MessageType.LOG
    message: str
    level: LogLevel = LogLevel.INFO
    source: str = "model"
    phase: str = "run"
    #: False = written durably, not sent to the browser live (raw solver
    #: chatter kept for offline tooling). Filters the *live send* only.
    client_visible: bool = True


class ProgressMessage(_Common):
    """One sampled point on "how is this run doing". Sampled, not per-iteration."""

    type: Literal[MessageType.PROGRESS] = MessageType.PROGRESS
    elapsed_seconds: float = Field(ge=0)
    percent_complete: float | None = Field(default=None, ge=0, le=100)
    primary_metric: float | None = None
    primary_metric_label: str | None = None
    #: Model-specific extras a generic progress view ignores and a
    #: model-specific view can grow into later.
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("primary_metric", mode="after")
    @classmethod
    def _finite_metric(cls, v: float | None) -> float | None:
        return sanitize_metric(v)


class StatusMessage(_Common):
    """A lifecycle transition — a notification, not the record of truth.

    The record of truth is the ``run_status`` row; this announces a change to
    it. See ``docs/architecture.md``.
    """

    type: Literal[MessageType.STATUS] = MessageType.STATUS
    status: RunStatus
    detail: str | None = None


class ResultMessage(_Common):
    """Not best-effort. Written whenever the model reaches results, whatever
    the terminal status turns out to be.

    Carries a pointer and a preview, never the result set itself: the full
    data lives in the model's own UC table, governed by its own grants.
    """

    type: Literal[MessageType.RESULT] = MessageType.RESULT
    #: Downsampled (LTTB for time-series-shaped results — see
    #: ``shared.downsample``), bounded at ``PREVIEW_MAX_POINTS``.
    preview: list[dict[str, Any]] = Field(default_factory=list)
    #: Rows *actually written* to the durable results table. Always populated,
    #: including 0 — this is what distinguishes "succeeded, wrote 8,760 rows"
    #: from "succeeded, wrote nothing because the write failed".
    row_count: int = Field(ge=0)
    #: Enough for a client to pull the full set on demand (table, keys).
    fetch_hint: dict[str, Any] = Field(default_factory=dict)

    # --- extension for incremental results (see the spec's changelog) -----
    #: Which chunk of a multi-emission run this is. 0 for the common
    #: once-at-the-end case. Distinct from ``seq``, which counts *all*
    #: messages: two result chunks are chunk 0 and 1 but may be seq 40 and 91.
    chunk_index: int = Field(default=0, ge=0)
    #: False while more chunks are still coming. A run's results are complete
    #: when a message with ``final=True`` has been seen.
    final: bool = True

    @field_validator("preview", mode="after")
    @classmethod
    def _bounded_preview(cls, v: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if len(v) > PREVIEW_MAX_POINTS:
            raise ValueError(
                f"preview has {len(v)} points, limit is {PREVIEW_MAX_POINTS}; "
                "downsample it (shared.downsample.lttb) rather than truncating"
            )
        return v


Message = Annotated[
    Union[LogMessage, ProgressMessage, StatusMessage, ResultMessage],
    Field(discriminator="type"),
]

#: The one entry point for turning untrusted bytes/dicts into a message.
#: Validation lives with whichever side is deserialising, never inside the
#: encoding step.
MessageAdapter: TypeAdapter[Message] = TypeAdapter(Message)

_BY_TYPE: dict[MessageType, type[_Common]] = {
    MessageType.LOG: LogMessage,
    MessageType.PROGRESS: ProgressMessage,
    MessageType.STATUS: StatusMessage,
    MessageType.RESULT: ResultMessage,
}


def make_message(
    type: str | MessageType,
    *,
    run_id: str,
    seq: int,
    ts: int | None = None,
    **fields: Any,
) -> Message:
    """Build a message from ``emit(type, **fields)``-shaped arguments.

    The job harness stamps ``run_id``/``seq``/``ts``; a model supplies only
    the type-specific fields and never sees these three.
    """
    try:
        mtype = MessageType(type)
    except ValueError:
        raise ValueError(
            f"unknown message type {type!r}; expected one of "
            f"{', '.join(m.value for m in MessageType)}"
        ) from None
    model = _BY_TYPE[mtype]
    return model(run_id=run_id, seq=seq, ts=now_ms() if ts is None else ts, **fields)
