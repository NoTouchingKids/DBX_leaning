"""Encoding — a delivery detail, deliberately kept outside the envelope.

msgpack job->app and in the Delta write buffer; JSON app->browser (native to
the browser, readable in devtools, already compressed by the transport).

The contract both codecs owe: **identical logical content**. If a message
round-trips through msgpack and through JSON and the two results differ, the
boundary between "protocol" and "serialisation" has been drawn in the wrong
place. ``tests/shared/test_codec.py`` asserts exactly that.
"""

from __future__ import annotations

import json
from typing import Any

import msgpack

from .envelope import Message, MessageAdapter

__all__ = [
    "to_jsonable",
    "encode_json",
    "decode_json",
    "encode_msgpack",
    "decode_msgpack",
    "encode_msgpack_many",
    "decode_msgpack_many",
]


def to_jsonable(msg: Message) -> dict[str, Any]:
    """Plain-dict form: enums as their string values, nothing exotic.

    Both codecs go through this, which is what makes them agree.
    """
    return msg.model_dump(mode="json")


def encode_json(msg: Message) -> bytes:
    return json.dumps(to_jsonable(msg), separators=(",", ":")).encode("utf-8")


def decode_json(raw: bytes | str) -> Message:
    return MessageAdapter.validate_python(json.loads(raw))


def encode_msgpack(msg: Message) -> bytes:
    return msgpack.packb(to_jsonable(msg), use_bin_type=True)


def decode_msgpack(raw: bytes) -> Message:
    return MessageAdapter.validate_python(msgpack.unpackb(raw, raw=False))


def encode_msgpack_many(msgs: list[Message]) -> bytes:
    """One frame holding many messages — how the Delta buffer holds a batch."""
    return msgpack.packb([to_jsonable(m) for m in msgs], use_bin_type=True)


def decode_msgpack_many(raw: bytes) -> list[Message]:
    return [MessageAdapter.validate_python(d) for d in msgpack.unpackb(raw, raw=False)]
