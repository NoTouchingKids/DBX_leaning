"""A tick a second — and the worked example of the model template.

This is what a model looks like now: three small methods and two class
attributes. There is no `attach`, no run loop, no cancel polling, no
percentage arithmetic and no interruptible sleep, because `modelkit.Model`
does all of that once for every model instead of each model doing it again.

It used to be sixty lines and almost none of them were about heartbeats. The
diff between then and now is the argument for the template.

It still writes no results and reads no data. That is not a simplification to
be fixed later — a model owns its own data lifecycle in v4, and a heartbeat
has none. `result` messages get exercised when the first real model lands.
"""

from __future__ import annotations

from typing import Any

from modelkit import Model

__all__ = ["Heartbeat", "build_model"]

#: Ticks per second. A real model samples progress a few times a second at
#: most (see the envelope spec); this sits at the slow end of that on purpose,
#: because the interesting question is whether a tick ARRIVES, not throughput.
DEFAULT_HZ = 1.0
DEFAULT_SECONDS = 60.0


class Heartbeat(Model):
    #: Reads correctly in a log line and in a chart legend: "60 ticks".
    unit = "ticks"

    def configs(self) -> dict[str, Any]:
        return {"seconds": DEFAULT_SECONDS, "hz": DEFAULT_HZ}

    def prestep(self) -> None:
        """Clamp the config, then say how much work there is.

        Setting `total` and `interval` here rather than in `configs` is the
        point of `prestep` existing: they are DERIVED from configuration, and a
        real model derives them from data it has to read first.
        """
        self.hz = max(0.01, float(self.hz))
        self.seconds = max(0.0, float(self.seconds))
        self.total = max(1, int(self.seconds * self.hz))
        self.interval = 1.0 / self.hz

    def step(self, i: int) -> dict[str, Any]:
        """One tick. The template emits the progress and does the waiting."""
        return {"tick": i + 1, "of": self.total}

    @property
    def ticks(self) -> int:
        """How many ticks have completed. The template counts them."""
        return self._count


def build_model(config: dict[str, Any] | None = None) -> Heartbeat:
    """Kept for callers that want a factory rather than the class.

    The entry point names `Heartbeat` directly — the template's `__init__`
    already takes both a config dict and keywords, so a separate factory has
    nothing left to do. This remains because `build_model` is the first name
    `job/loader.py` looks for, and a model author reading that list should find
    the obvious thing working.
    """
    return Heartbeat(config or {})
