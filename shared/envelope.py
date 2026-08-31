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
from typing import Annotated, Any, Literal, overload

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

__all__ = [
    "MessageType",
    "LogLevel",
    "RunStatus",
    "PLATFORM_STATUSES",
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
    handled where it is produced — see ``job/models/gurobi_scheduling``.
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


class RunStatus:
    """The platform's own statuses — **string constants, not an enum.**

    A status is a plain ``str`` on the wire, and a model may send one of its
    own: a fitting stage, a phase name, whatever that model's progress is
    actually made of. That is the point. A closed enum has to be re-declared in
    every language that touches the contract and goes stale the first time it
    gains a member — a cost paid by the frontend, the ingestion job, and
    anything written in a language the app is not.

    These six remain the convention every model should reach for first, and
    what the platform itself emits. They are a starting point, not a wall.
    """

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    INFEASIBLE = "INFEASIBLE"


#: The platform's six, for the places that only ever deal in those — the run
#: store's defaults, the Jobs API mapping. NOT a validation rule: a status
#: outside this set is legal on the wire.
PLATFORM_STATUSES = frozenset(
    {
        RunStatus.QUEUED,
        RunStatus.RUNNING,
        RunStatus.SUCCEEDED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
        RunStatus.INFEASIBLE,
    }
)

#: Which of the platform's six mean "finished".
#:
#: **Do not use this to decide whether a message is terminal.** It cannot
#: answer for a model-defined status, and reaching for it is how the closed
#: enum grows back somewhere less visible. `StatusMessage.terminal` is the
#: authority — the producer says so, nothing downstream infers it. This set is
#: for code that already knows it is holding one of the platform's own, such as
#: seeding a `run_status` row.
TERMINAL_STATUSES = frozenset(
    {
        RunStatus.SUCCEEDED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
        RunStatus.INFEASIBLE,
    }
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
    #: Free-form. `RunStatus` holds the platform's six as constants and every
    #: model should prefer them, but a model-defined status is legal and is how
    #: per-model categorical progress travels without a second field.
    status: str = Field(min_length=1)
    #: **Whether this is the last word on the run.** Stated by the producer,
    #: never inferred downstream — which is the whole thing that makes an open
    #: `status` safe rather than merely convenient. The harness knows to stop,
    #: the app knows to close the stream, and the client knows to stop waiting,
    #: without any of them holding a list of which strings mean "finished".
    terminal: bool = False
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
    LogMessage | ProgressMessage | StatusMessage | ResultMessage,
    Field(discriminator="type"),
]

#: The one entry point for turning untrusted bytes/dicts into a message.
#: Validation lives with whichever side is deserialising, never inside the
#: encoding step.
MessageAdapter: TypeAdapter[Message] = TypeAdapter(Message)

_BY_TYPE: dict[MessageType, type[Message]] = {
    MessageType.LOG: LogMessage,
    MessageType.PROGRESS: ProgressMessage,
    MessageType.STATUS: StatusMessage,
    MessageType.RESULT: ResultMessage,
}


# Overloads so the concrete type is known at the call site: `make_message(
# "result", ...).row_count` should typecheck, and a reader (or an editor)
# should not have to widen to the union to find out what came back.
@overload
def make_message(
    type: Literal["log", MessageType.LOG],
    *,
    run_id: str,
    seq: int,
    ts: int | None = None,
    **fields: Any,
) -> LogMessage: ...


@overload
def make_message(
    type: Literal["progress", MessageType.PROGRESS],
    *,
    run_id: str,
    seq: int,
    ts: int | None = None,
    **fields: Any,
) -> ProgressMessage: ...


@overload
def make_message(
    type: Literal["status", MessageType.STATUS],
    *,
    run_id: str,
    seq: int,
    ts: int | None = None,
    **fields: Any,
) -> StatusMessage: ...


@overload
def make_message(
    type: Literal["result", MessageType.RESULT],
    *,
    run_id: str,
    seq: int,
    ts: int | None = None,
    **fields: Any,
) -> ResultMessage: ...


@overload
def make_message(
    type: str | MessageType,
    *,
    run_id: str,
    seq: int,
    ts: int | None = None,
    **fields: Any,
) -> Message: ...


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
    return model(run_id=run_id, seq=seq, ts=now_ms() if ts is None else ts, **fields)  # type: ignore[return-value]
