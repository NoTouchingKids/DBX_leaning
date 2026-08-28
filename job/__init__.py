"""The job harness: loads a model, drives it, and gets its messages out —
live over the WebSocket where one exists, durable always.

Knows nothing about FastAPI, SSE, or the browser. Knows nothing about any
specific model either: what varies between models is handled by capability
detection in ``job.loader`` and ``job.drivers``, never by branching on which
model is running.
"""

from .bus import WebSocketBus
from .cancellation import CancellationToken
from .config import JobConfig
from .emitter import Emitter
from .lakebase import LakebaseStatus
from .loader import ModelHandle, ModelLoadError, load_model
from .record import RunRecord
from .runner import JobHarness, RunOutcome, run_job

__all__ = [
    "CancellationToken",
    "JobConfig",
    "Emitter",
    "LakebaseStatus",
    "ModelHandle",
    "ModelLoadError",
    "RunRecord",
    "WebSocketBus",
    "load_model",
    "JobHarness",
    "RunOutcome",
    "run_job",
]
