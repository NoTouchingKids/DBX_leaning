"""The job harness — mid-rewrite. See `docs/v4-rewrite-plan.md`.

What survives from v3 is here: config, credentials, cancellation, and the
duck-typed model loader. Those four were not the problem and are being ported
rather than redesigned.

What is missing is the whole transport and durability layer — `runner`,
`emitter`, `relay`, `channels`, `sink`, `buffer`, `delta`, `drivers` — because
v4 replaces all of it at once rather than in pieces:

  - threaded rather than asyncio, which deletes the ipykernel workaround
    instead of working around it;
  - one RPC-shaped channel over the WebSocket, not four paths with four
    failure semantics;
  - write-through to a Unity Catalog volume, not an in-memory buffer flushed
    to Delta through Spark;
  - and no table writes at all, because a model now owns its own results.

The v3 implementations are on `dev`, deployed and working. Read them for what
they learned — the comments are the point — rather than porting them, since
almost every one is an answer to a question v4 asks differently.

Slice 1 rebuilds this. Until then `job` is a package with no harness in it,
and that is deliberate: a half-migrated transport that still imports the old
one is how two designs end up shipping at once.
"""

from .cancellation import CancellationToken
from .config import JobConfig
from .loader import ModelHandle, ModelLoadError, load_model

__all__ = [
    "CancellationToken",
    "JobConfig",
    "ModelHandle",
    "ModelLoadError",
    "load_model",
]
