"""Shared model template, installed as a serverless environment dependency.

    from modelkit import Model

See `base.py` for the contract and `models/heartbeat/` for a worked example.
Nothing in the platform imports this package — `job/loader.py` finds a model
structurally, so the template is a convenience and never a requirement.
"""

from .base import CANCELLED, FAILED, STOP, SUCCEEDED, Model

__all__ = ["Model", "STOP", "SUCCEEDED", "CANCELLED", "FAILED"]
