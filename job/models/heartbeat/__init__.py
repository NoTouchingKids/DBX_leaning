"""The heartbeat: a model that emits a tick a second and nothing else.

Slice 1's whole purpose. It exists so the transport can be proven with no
model in the picture — when a tick reaches a browser from a deployed job and
the part files are on the volume, the platform exists.

Everything a real model does and this does not — read a table, write results,
take more than a moment to think — is deliberately absent. If something breaks
during Slice 1 it is the transport, because there is nothing else here.
"""

from .model import Heartbeat, build_model

__all__ = ["Heartbeat", "build_model"]
