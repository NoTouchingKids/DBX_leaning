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

from .envelope import PLATFORM_STATUSES, TERMINAL_STATUSES, MessageAdapter
from .rpc import JSONRPC_VERSION, Method

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
        # The platform's own statuses, and which of them mean "finished".
        #
        # ADVISORY, not a contract. `status` is an open string so a model may
        # send its own, and a client must read `status.terminal` to know
        # whether anything further arrives — never match against this list.
        # It is published so a UI can label and order the common six, which is
        # the one thing it is genuinely good for.
        "x-platform-statuses": sorted(PLATFORM_STATUSES),
        "x-terminal-statuses": sorted(TERMINAL_STATUSES),
        **schema,
    }


def control_schema() -> dict[str, Any]:
    """The job<->app RPC surface: the methods, and what each is for.

    Not a Pydantic-derived shape any more. v3's control frames were a closed
    set of Pydantic models, so a schema could be generated from them; a
    JSON-RPC method set is a vocabulary, and the useful thing to publish is
    which methods exist, which are notifications, and which direction each
    goes. A browser never sees any of it — this is here so a future non-Python
    harness has something to build against without reading `job/ws.py`.
    """
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"{_BASE_ID}/control.schema.json",
        "title": "DBX_leaning job/app RPC methods",
        "description": ("JSON-RPC 2.0 over the job's WebSocket. Generated from shared/rpc.py."),
        "x-schema-version": SCHEMA_VERSION,
        "x-jsonrpc-version": JSONRPC_VERSION,
        "x-methods": {
            Method.TELEMETRY: {
                "kind": "notification",
                "from": "job",
                "summary": "A batch of envelope messages. The bulk of all traffic.",
            },
            Method.HELLO: {
                "kind": "request",
                "from": "job",
                "summary": "Which run this is, and the seq it is picking up from.",
            },
            Method.BYE: {
                "kind": "notification",
                "from": "job",
                "summary": "Clean shutdown; distinct from a dropped socket.",
            },
            Method.CANCEL: {
                "kind": "request",
                "from": "app",
                "summary": "Ask the job to stop. Answered, which v3 could not do.",
            },
            Method.REPLAY: {
                "kind": "request",
                "from": "app",
                "summary": "Resend [from_seq, to_seq]. The only live backfill path.",
            },
            Method.PING: {
                "kind": "request",
                "from": "either",
                "summary": "Application-level keepalive, not a WS protocol ping.",
            },
        },
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
