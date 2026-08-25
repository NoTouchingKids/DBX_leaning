"""JSON Schema for the wire protocol, derived from the Pydantic models.

The frontend needs the envelope's shape and its enums. Hand-writing that
would create a second source of truth and, eventually, a drift bug of the
kind this repo has already paid for twice. So it is *generated* from
``shared/envelope.py`` and ``shared/protocol.py``, committed under
``schema/`` so it is reviewable and diffable, and checked in CI-shaped tests
so the committed copy cannot fall behind the code.

**Serialization mode, deliberately.** The schema describes what actually goes
*out* over SSE — enums as their string values, no Python-side coercions — not
what the server is willing to accept. A client validating against it is
validating the thing it will really receive.

What a frontend does with it:

- ``json-schema-to-typescript`` or ``quicktype`` turns the discriminated
  ``oneOf`` into a TypeScript union keyed on ``type``, so narrowing on
  ``msg.type`` is checked by the compiler.
- The enums (``LogLevel``, ``RunStatus``) become string-literal unions rather
  than being retyped by hand in the client.
- ``ajv`` can validate at runtime in development, where a message that does
  not match is a bug worth failing loudly on.
"""

from __future__ import annotations

from typing import Any

from .envelope import TERMINAL_STATUSES, MessageAdapter
from .protocol import ControlFrame

__all__ = ["SCHEMA_VERSION", "envelope_schema", "control_schema", "protocol_schema"]

#: Bumped when the protocol changes shape in a way a client must notice.
#: Not the package version: a client cares about the wire, not the release.
SCHEMA_VERSION = "1.0.0"

_BASE_ID = "https://github.com/NoTouchingKids/DBX_leaning/schema"


def envelope_schema() -> dict[str, Any]:
    """The four run messages, as one discriminated union."""
    schema = MessageAdapter.json_schema(mode="serialization")
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"{_BASE_ID}/envelope.schema.json",
        "title": "DBX_leaning run message envelope",
        "description": (
            "Everything a run produces: log, progress, status, result. One shape, "
            "discriminated on `type`, identical over WebSocket, HTTP push and Delta. "
            "Generated from shared/envelope.py — do not edit by hand."
        ),
        "x-schema-version": SCHEMA_VERSION,
        # Not derivable from the shape: which statuses mean "nothing further
        # arrives". A client uses it to close streams and stop polling, so it
        # travels with the schema rather than being retyped on the far side.
        "x-terminal-statuses": sorted(s.value for s in TERMINAL_STATUSES),
        **schema,
    }


def control_schema() -> dict[str, Any]:
    """The job<->app control frames. Not run telemetry — connection management.

    A browser never sees these; they are here so the protocol is documented
    in one place and a future non-Python job harness has something to build
    against.
    """
    schema = ControlFrame.model_json_schema(mode="serialization")
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"{_BASE_ID}/control.schema.json",
        "title": "DBX_leaning job/app control frame",
        "description": (
            "hello / hello_ack / cancel / ping / pong / bye, exchanged between a job "
            "and the app over the WebSocket. Generated from shared/protocol.py."
        ),
        "x-schema-version": SCHEMA_VERSION,
        **schema,
    }


def protocol_schema() -> dict[str, Any]:
    """Both, in one document — what the app serves at ``/api/schema``."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"{_BASE_ID}/protocol.schema.json",
        "title": "DBX_leaning wire protocol",
        "x-schema-version": SCHEMA_VERSION,
        "$defs": {
            "envelope": envelope_schema(),
            "control": control_schema(),
        },
    }
