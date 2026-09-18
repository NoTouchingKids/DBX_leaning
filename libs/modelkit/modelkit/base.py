"""The model template: implement `step`, get a Databricks job.

A model used to hand-roll its own run loop. The heartbeat's was sixty lines and
almost none of it was about heartbeats — it was cancel polling, percentage
arithmetic, an interruptible sleep, a start log, a done log, and the rule that
a cancel returns ``CANCELLED``. Every model would have written that again, and
the interesting ones would have written it slightly differently.

This is that loop, once. An author declares configuration, does setup, and
writes one `step`::

    from modelkit import Model

    class Yours(Model):
        unit = "iterations"

        def configs(self):
            return {"iterations": 100, "tolerance": 1e-6}

        def prestep(self):
            self.total = self.iterations
            self.solver = build_something(self.tolerance)

        def step(self, i):
            gap = self.solver.advance()
            return {"metric": gap, "label": "gap"}

## How this reaches a Databricks job

It does not, and that is the point — **the platform never imports this
module.** ``job/loader.py`` discovers a model structurally, by looking for
``attach`` and ``run`` on whatever object it is handed. This class provides
both, so a subclass satisfies the harness without the harness knowing this file
exists. A model that outgrows the template overrides ``run()`` and is still a
model; one that never uses the template at all is still a model.

So the inheritance is a CONVENIENCE, not the contract. ``models/README.md``
has the contract, and it is still duck-typed.

## Where this is installed from

Not from PyPI, and not as a dependency of each model. It is a **shared
serverless environment dependency** — the pattern in
``docs/docs-databricks-com-aws-en-compute-serverless-dependencies.md`` under
"Create common tools to share across your workspace": a workspace folder with
a ``pyproject.toml``, added once to an environment, imported by everything in
it.

That is why ``models/heartbeat/pyproject.toml`` still says
``dependencies = []``. The kit is provided by the environment, exactly as
``pyspark`` is — and for the same reason a model does not declare pyspark.

This module imports nothing but the standard library, so it costs an
environment nothing to carry.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

__all__ = ["Model", "STOP", "SUCCEEDED", "CANCELLED", "FAILED"]

#: The three the template itself can return. A model may name its own status —
#: the envelope's `status` field is deliberately open — but these are the ones
#: this loop produces on its own.
SUCCEEDED = "SUCCEEDED"
CANCELLED = "CANCELLED"
FAILED = "FAILED"


class _Stop:
    """Sentinel. A `step` returning this ends the loop cleanly."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "STOP"


#: Return from `step` to finish early and successfully — the way a solver that
#: converges before its iteration cap does. Distinct from a cancel, which is
#: someone asking it to stop, and from an exception, which is a failure.
STOP = _Stop()


def _ignore(*_args: Any, **_kwargs: Any) -> None:
    """Where messages go before `attach`.

    A model must be runnable with nothing listening — that is the normal case,
    not a degraded one — so this is a no-op rather than an error.
    """


class Model(ABC):
    """Subclass this. Implement `step`. Everything else has a default."""

    # --- what an author declares -------------------------------------------

    #: What one step IS, for progress labels: "ticks", "iterations", "epochs".
    #: Plural, because it reads correctly in "142 iterations" and in a legend.
    unit: str = "steps"

    #: How many steps. `None` means "unknown" — the loop then runs until `step`
    #: returns `STOP`, and progress carries no percentage, because a made-up
    #: percentage is worse than an absent one.
    total: int | None = None

    #: Seconds to wait between steps. Zero means as fast as the work allows.
    #: The wait is interruptible: a cancel is noticed within ~100ms rather than
    #: after a whole interval.
    interval: float = 0.0

    def configs(self) -> dict[str, Any]:
        """Default configuration, as a plain dict.

        ``DBX_MODEL_CONFIG`` (and any keyword passed to the constructor)
        overrides these, and the result is set as attributes: a ``configs``
        returning ``{"iterations": 100}`` gives you ``self.iterations``.

        Declaring defaults here rather than in ``__init__`` is what lets a
        subclass be constructed with no arguments at all, which is what the
        harness does when a job supplies no config.
        """
        return {}

    def prestep(self) -> None:  # noqa: B027 - an OPTIONAL hook, deliberately not abstract
        """One-time setup, before the first step and inside the run.

        Read data, build a solver, set ``self.total``. It runs INSIDE the run
        rather than in ``__init__`` so that whatever it emits or raises is part
        of the run's record — a model that fails while loading its data has
        failed a run, not failed to construct.
        """

    @abstractmethod
    def step(self, i: int) -> Any:
        """One unit of work. ``i`` counts from zero.

        Return ``None`` for a plain step, ``STOP`` to finish early, or a dict
        that becomes the progress payload. Two keys in that dict are special,
        because every model wants them and none should have to plumb them:

            metric   the number worth charting  (default: the step count)
            label    what that number is        (default: ``self.unit``)

        Everything else in the dict travels as ``payload``, which a generic
        progress view ignores and a model-specific one can grow into.
        """

    def poststep(self, status: str) -> None:  # noqa: B027 - optional, see prestep
        """Teardown, after the last step, whatever the outcome.

        Runs on the cancelled and failed paths too, which is what makes it the
        right place to write results: a cancelled run keeps its incumbent, and
        ``status`` tells you which case you are in.
        """

    # --- what the template provides ----------------------------------------

    def __init__(self, config: dict[str, Any] | None = None, **overrides: Any) -> None:
        """Both call shapes, because both have a caller.

        ``Model({"hz": 2})`` is what ``job/loader.py`` does with
        ``DBX_MODEL_CONFIG``; ``Model(hz=2)`` is what a person does in a
        notebook. Neither should have to know about the other.
        """
        merged = {**self.configs(), **(config or {}), **overrides}

        for key, value in merged.items():
            # Refuse to shadow the template's own machinery. A config key
            # called `step` or `run` would replace the method with a number and
            # fail later, somewhere unrelated, with a message about the number.
            if callable(getattr(type(self), key, None)):
                raise TypeError(
                    f"config key {key!r} would shadow {type(self).__name__}.{key}(); "
                    f"rename the config key"
                )
            setattr(self, key, value)

        #: The merged configuration, kept whole. `prestep` often wants to report
        #: what it was given, and reconstructing that from attributes is
        #: guesswork once defaults and overrides have been flattened together.
        self.config: dict[str, Any] = merged

        self._emit: Callable[..., Any] = _ignore
        self._should_cancel: Callable[[], bool] = lambda: False
        self._started: float = 0.0
        self._count: int = 0

    # The harness calls this and nothing else to wire a model up. Note what is
    # NOT here: no socket, no volume, no Databricks. A model never learns what
    # is on the other end of `emit`, which is why the same object runs in a
    # notebook, in a test and in a job without knowing which.
    def attach(self, emit: Callable[..., Any], should_cancel: Callable[[], bool]) -> None:
        self._emit = emit or _ignore
        self._should_cancel = should_cancel or (lambda: False)

    @property
    def name(self) -> str:
        """What this model calls itself in logs. Override for a nicer one."""
        return type(self).__name__

    @property
    def cancelled(self) -> bool:
        """Has someone asked this run to stop. Poll it in long work.

        The template polls between steps; a `step` that takes minutes should
        poll inside itself, which is what a solver callback is for. v3's CP-SAT
        model learned this the hard way — a callback firing only on improvement
        left a cancel unseen for the whole time limit.
        """
        return bool(self._should_cancel())

    def log(self, message: str, *, level: str = "INFO", phase: str = "run", **fields: Any) -> None:
        """Best-effort commentary. Dropped on the live path under pressure,
        never on the durable one."""
        self._emit("log", message=message, level=level, phase=phase, **fields)

    def progress(
        self,
        *,
        count: int | None = None,
        metric: float | None = None,
        label: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """One sampled point.

        The template calls this after every step, so most models never call it
        directly — do so when one step contains several things worth reporting.
        """
        done = self._count if count is None else count
        percent = None
        if self.total:
            percent = min(100.0, 100.0 * done / self.total)

        self._emit(
            "progress",
            elapsed_seconds=max(0.0, time.monotonic() - self._started),
            percent_complete=percent,
            primary_metric=float(done if metric is None else metric),
            primary_metric_label=label or self.unit,
            payload=payload or {},
        )

    def sleep(self, seconds: float) -> bool:
        """Wait, but notice a cancel. ``False`` means one arrived.

        Sliced rather than one long ``time.sleep``, so a run cancelled during a
        thirty-second wait stops in a tenth of a second rather than thirty.
        """
        deadline = time.monotonic() + max(0.0, seconds)
        while time.monotonic() < deadline:
            if self.cancelled:
                return False
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
        return not self.cancelled

    def run(self) -> str:
        """The loop. Override it if your model does not have this shape.

        Returns the status it believes the run reached. The harness gets the
        last word — a cancel it observed and a cancel this missed must not
        produce different terminal statuses for the same event — but a model
        saying what it believes is how a model-defined status reaches the wire.
        """
        self._started = time.monotonic()
        self.prestep()

        scope = f": {self.total} {self.unit}" if self.total else ""
        self.log(f"{self.name} starting{scope}")

        status = SUCCEEDED
        try:
            i = 0
            while self.total is None or i < self.total:
                if self.cancelled:
                    self.log(f"cancel observed after {self._count} {self.unit}", level="WARNING")
                    status = CANCELLED
                    break

                outcome = self.step(i)
                if outcome is STOP:
                    self.log(f"{self.name} stopped early after {self._count} {self.unit}")
                    break

                self._count = i = i + 1
                payload = dict(outcome) if isinstance(outcome, dict) else {}
                self.progress(
                    metric=payload.pop("metric", None),
                    label=payload.pop("label", None),
                    payload=payload,
                )

                if self.interval and not self.sleep(self.interval):
                    self.log(f"cancel observed after {self._count} {self.unit}", level="WARNING")
                    status = CANCELLED
                    break
        except Exception:
            # `poststep` still runs — a model that produced something partial
            # should get its chance to keep it — and the harness turns the
            # exception into a FAILED run with the traceback.
            self.poststep(FAILED)
            raise

        self.poststep(status)
        self.log(f"{self.name} finished: {self._count} {self.unit} ({status})")
        return status
