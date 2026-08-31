"""The message envelope and protocol helpers — the one contract every other
track builds against.

Imported by ``app/`` and ``job/``. **Not** imported by ``job/models/``: a model
conforms to the envelope's documented shape via the ``emit()`` callback it is
handed, and needs no import from the platform to run standalone.
"""

from .codec import (
    decode_json,
    decode_msgpack,
    decode_msgpack_many,
    encode_json,
    encode_msgpack,
    encode_msgpack_many,
    to_jsonable,
)
from .downsample import downsample_rows, lttb
from .envelope import (
    PREVIEW_MAX_POINTS,
    TERMINAL_STATUSES,
    LogLevel,
    LogMessage,
    Message,
    MessageAdapter,
    MessageType,
    ProgressMessage,
    ResultMessage,
    RunStatus,
    StatusMessage,
    make_message,
    now_ms,
    sanitize_metric,
)
from .rpc import ErrorCode, Method, RpcError, notification, parse, request
from .seq import SeqCounter
from .tables import TableSet, table_for, to_row

__all__ = [
    "LogLevel",
    "LogMessage",
    "Message",
    "MessageAdapter",
    "Method",
    "ErrorCode",
    "RpcError",
    "notification",
    "parse",
    "request",
    "MessageType",
    "ProgressMessage",
    "ResultMessage",
    "RunStatus",
    "StatusMessage",
    "TERMINAL_STATUSES",
    "PREVIEW_MAX_POINTS",
    "make_message",
    "now_ms",
    "sanitize_metric",
    "to_jsonable",
    "encode_json",
    "decode_json",
    "encode_msgpack",
    "decode_msgpack",
    "encode_msgpack_many",
    "decode_msgpack_many",
    "SeqCounter",
    "TableSet",
    "table_for",
    "to_row",
    "lttb",
    "downsample_rows",
]
