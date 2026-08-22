"""The FastAPI application — the optional observer.

Jobs run whether or not this is up. When it is, it accepts a job's WebSocket,
relays to browsers over SSE, and serves backfill reads. It never becomes the
thing a run depends on to make progress.
"""

from .config import AppConfig
from .main import create_app
from .services import ServiceHub

__all__ = ["AppConfig", "ServiceHub", "create_app"]
