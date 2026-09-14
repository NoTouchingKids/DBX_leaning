"""Encoding — a delivery detail, deliberately kept outside the envelope.

**JSON, everywhere.** v3 used msgpack job->app and in the Delta write buffer,
and JSON only to the browser. v4 has one codec, and it follows from two
decisions rather than being a preference of its own: the wire is JSON-RPC, and
the durable records are files that `replay` reads back — so an operator opening
a telemetry part file and a `replay` parsing the same bytes are both worth more
here than smaller frames. Telemetry records are small and the transport
compresses.

The msgpack helpers that used to live here are gone with the dependency. What
remains is `to_jsonable`, which is the part that was actually doing the work:
turning a Pydantic message into plain data, once, so that whatever encodes it
never has to know what a `Message` is.
"""

from __future__ import annotations

import json
from typing import Any

from .envelope import Message, MessageAdapter

__all__ = [
    "to_jsonable",
    "encode_json",
    "decode_json",
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
