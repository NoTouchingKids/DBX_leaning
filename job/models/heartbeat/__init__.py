"""A run that does nothing, on purpose, for as long as you ask — the model
that exists so the live path has something slow enough to watch."""

from .model import HeartbeatModel, build_model

__all__ = ["HeartbeatModel", "build_model"]
