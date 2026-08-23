"""The job harness: loads a model, drives it, and gets its messages onto
every channel — live where one exists, durable always.

Knows nothing about FastAPI, SSE, or the browser. Knows nothing about any
specific model either: what varies between models is handled by capability
detection in ``job.loader`` and ``job.drivers``, never by branching on which
model is running.
"""

from .cancellation import CancellationToken
from .config import JobConfig
from .emitter import Emitter
from .loader import ModelHandle, ModelLoadError, load_model
from .runner import JobHarness, RunOutcome, run_job

__all__ = [
    "CancellationToken",
    "JobConfig",
    "Emitter",
    "ModelHandle",
    "ModelLoadError",
    "load_model",
    "JobHarness",
    "RunOutcome",
    "run_job",
]
