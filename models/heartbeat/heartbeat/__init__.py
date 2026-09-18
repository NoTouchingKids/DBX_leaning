"""The heartbeat model: a tick a second, and nothing else.

**This package depends on nothing.** Not the harness, not the envelope, not
Databricks. That is the point of a model being its own distribution rather
than a subpackage of `job` — importing it in a notebook loads this file and
`model.py` and stops.

    from heartbeat import Heartbeat

    m = Heartbeat(seconds=10)
    m.attach(emit=print, should_cancel=lambda: False)
    m.run()

The harness finds it through the `dbx_leaning.models` entry point declared in
`pyproject.toml`, so nothing has to know where this directory is. A model in
another repository entirely works the same way.
"""

from .model import Heartbeat, build_model

__all__ = ["Heartbeat", "build_model"]
