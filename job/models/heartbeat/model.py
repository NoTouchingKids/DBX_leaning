"""A run that does nothing, on purpose, for as long as you ask.

**Why this model exists.** Every other model here is too fast to watch. Not
because the algorithms are trivial — because on serverless the wall clock is
dominated by everything that is not the model: the Spark session comes up, the
environment installs, and by the time the WebSocket has been dialled, the
handshake accepted and a browser has subscribed, a scenario sweep or a
conjugate A/B has already finished and the run is terminal. There is nothing
left to observe. Watching the live path required getting lucky with timing, or
inflating a real model's config until it was slow for reasons that had nothing
to do with what it computes.

So this one has no algorithm at all. It emits logs, progress and phase changes
on a wall clock for a duration you choose — ten minutes by default. It exists
to be attached to, watched, filtered, backfilled and cancelled while it is
still running.

**It writes no results, and that is the point.** No `results_table`, no
`results()`, no result message — the only model in the lineup with none, and
the reason it is exempt in `tests/deploy/test_bundle.py` and
`app/client/.../registry.test.ts`. What it is testing is the LIVE path: the
socket staying up, messages arriving in order, a gap backfilling, a cancel
travelling back. A results table would add a Delta write and a Unity Catalog
dependency to a run whose whole value is that it needs neither — and a
diagnostic that can fail for reasons unrelated to what it diagnoses is worse
than no diagnostic. The harness still writes its own durable telemetry (logs,
progress, `run_events`); that is the floor, and it is not this model's to
turn off.

**What it is for, concretely.** Every one of these needs a run that is still
alive when you get there:

- the socket surviving the Databricks Apps ingress for minutes rather than the
  seconds an ingress probe measures;
- SSE reaching a browser, and a tab that reconnects mid-run backfilling the gap
  from the job's replay ring rather than the warehouse;
- cancel travelling client -> app -> job over the same socket, and the run
  ending CANCELLED with its incumbent results intact;
- the durable path flushing on the 30s age bound rather than the 1 MB size
  bound, which no fast model reaches;
- `run_status` in Lakebase moving through its transitions while something is
  watching the row.

**Its telemetry shape, and why it is not a config on an existing model.** The
other ten report progress that their work produces: a gap closes, a loss
falls, R-hat settles, units complete. The rate is a consequence of the
algorithm and cannot be chosen. Here the rate *is* the configuration —
`log_interval_seconds` and `progress_interval_seconds` are independent knobs,
and `logs_per_tick` bursts the bus on demand. It is the only model whose
`percent_complete` is exact and linear, because elapsed over duration is
genuinely all it is; and the only one that can be asked to run for a
known number of minutes.

**`primary_metric` is the model's own lateness.** There is no objective to
report, so the headline number is `tick_lag_seconds`: how far past its
scheduled time each progress tick actually landed. That is not filler — it is
the useful measurement of a soak run. A lag that grows says the job is being
starved, the emit path is blocking, or the bus is backed up behind a slow
consumer, and it is visible on the same generic progress view every other
model uses.

**No dependencies, no data, no randomness that matters.** Standard library
only, like `annealing`, and unlike `annealing` it does not even read
`models._data` — there is no dataset to read, and with no results table there
is nowhere provenance would go.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from typing import Any

__all__ = ["HeartbeatModel", "build_model"]

#: Ten minutes: long enough that a person can trigger a run, open the UI, watch
#: it stream, disconnect a tab to force a backfill, and still have time to
#: cancel it before it ends on its own.
DEFAULT_DURATION_S = 600.0

#: A hard ceiling on `duration_seconds`, clamped rather than rejected. This
#: model holds one of FIVE concurrent task slots for the whole account while it
#: runs (`CLAUDE.md`), and it is the one model whose runtime is a number
#: somebody types. A fat-fingered 60000 would hold a slot for sixteen hours;
#: clamping costs a log line and the run still does what was meant.
#:
#: Kept comfortably under `timeout_seconds` in `resources/model_heartbeat.job.yml`,
#: so the model always ends its own run. A Databricks timeout kills the task
#: instead, and a killed task reports no terminal status at all.
MAX_DURATION_S = 1800.0

#: How often the loop wakes, regardless of when the next log or progress tick
#: is due. This is the cancellation latency, and it is the number that matters
#: most in a model built to have cancel tested against it: a run that takes
#: five seconds to notice reads as a broken cancel button.
POLL_S = 0.25

#: The default phases. Named stages exist so there is something to *change*
#: during a run — a phase transition is a visible event on the log stream and a
#: field on every progress payload, which is what a client's grouping and
#: filtering have to be tested against.
DEFAULT_PHASES = ("warmup", "steady", "cooldown")

#: Every Nth log is emitted at this level instead of INFO, so a client's level
#: filter has something to filter. Deterministic, not random: a test can say
#: which log lines will be WARNING.
DEBUG_EVERY = 10
WARNING_EVERY = 25


def _positive(config: dict[str, Any], key: str, default: float) -> float:
    """A config number that must be > 0, or a readable error.

    Raised rather than clamped, and the asymmetry with `duration_seconds` is
    deliberate: a duration that is too big still runs the model, just for
    longer than intended, while an interval of 0 or -1 is not a run that went
    wrong — it is a loop with no meaning, and it should fail in `build()`
    before a task slot is spent on it.
    """
    value = config.get(key, default)
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be a number, got {value!r}") from None
    if not (value > 0) or math.isinf(value):
        raise ValueError(f"{key} must be a finite number greater than 0, got {value!r}")
    return value


class HeartbeatModel:
    """Emits telemetry on a timer for a fixed duration. Computes nothing."""

    #: No `results_table` and no `results()`, deliberately — see the module
    #: docstring. The harness emits no `result` message at all for a model that
    #: exposes neither (`job/runner.py::_collect_results`), which is exactly
    #: what is wanted: nothing to write, nothing to fail.

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = dict(config or {})
        self.emit: Callable[..., Any] | None = None
        self.should_cancel: Callable[[], bool] | None = None

        # Set in build(); named here so the object is readable before it.
        self.duration_s = DEFAULT_DURATION_S
        self.log_interval_s = 5.0
        self.progress_interval_s = 10.0
        self.logs_per_tick = 1
        self.quiet_every = 0
        self.phases: tuple[str, ...] = DEFAULT_PHASES
        self.seed = 0

        self.logs_emitted = 0
        self.progress_emitted = 0
        self.elapsed_s = 0.0
        self.cancelled = False

    # --- wiring ----------------------------------------------------------

    def _log(self, message: str, *, level: str = "INFO", phase: str, visible: bool = True) -> None:
        if self.emit is None:
            return
        self.emit(
            "log", message=message, level=level, source="model", phase=phase, client_visible=visible
        )

    def _phase_at(self, elapsed: float) -> tuple[int, str]:
        """Which named stage `elapsed` falls in. Equal spans, last one absorbs
        the remainder so a rounding error cannot index past the end."""
        span = self.duration_s / len(self.phases)
        index = min(int(elapsed // span), len(self.phases) - 1)
        return index, self.phases[index]

    # --- the contract ----------------------------------------------------

    def build(self) -> None:
        """Validate the config and say what the run will do before it does it."""
        requested = self.config.get("duration_seconds", DEFAULT_DURATION_S)
        try:
            requested = float(requested)
        except (TypeError, ValueError):
            raise ValueError(f"duration_seconds must be a number, got {requested!r}") from None
        if not (requested > 0):
            raise ValueError(f"duration_seconds must be greater than 0, got {requested!r}")
        self.duration_s = min(requested, MAX_DURATION_S)

        self.log_interval_s = _positive(self.config, "log_interval_seconds", 5.0)
        self.progress_interval_s = _positive(self.config, "progress_interval_seconds", 10.0)
        self.logs_per_tick = max(1, int(self.config.get("logs_per_tick", 1)))
        # 0 = every log goes to the browser. Above 0, every Nth is written
        # durably but withheld from the live stream, which is the only way to
        # exercise `client_visible=False` end to end — the flag a backfill has
        # to honour as well as the live send.
        self.quiet_every = max(0, int(self.config.get("quiet_every", 0)))
        self.seed = int(self.config.get("seed", 7))

        phases = self.config.get("phases") or DEFAULT_PHASES
        self.phases = tuple(str(p) for p in phases) or DEFAULT_PHASES

        if self.duration_s < requested:
            self._log(
                f"duration_seconds {requested:.0f} exceeds the {MAX_DURATION_S:.0f}s ceiling "
                f"and was clamped; this run holds one of five account-wide task slots",
                level="WARNING",
                phase="build",
            )
        self._log(
            f"heartbeat plan: {self.duration_s:.0f}s across {len(self.phases)} phases "
            f"({', '.join(self.phases)}), a log every {self.log_interval_s:g}s "
            f"x{self.logs_per_tick} and progress every {self.progress_interval_s:g}s; "
            f"computes nothing on purpose",
            phase="build",
        )

    def run(self) -> None:
        """Wake every POLL_S, emit whatever is due, stop on time or on cancel.

        Two independent schedules on one loop rather than two threads: the
        model is here to be *watched*, and a single loop means the emission
        order is the same every run and a lagging tick is visible as lag
        rather than hidden by another thread getting there first.

        Both schedules advance by fixed steps from the run's start rather than
        from the last tick, so a late tick does not push every later one back —
        the lag stays a measurement instead of becoming a drift.
        """
        started = time.monotonic()
        tick = 0
        next_log = 0.0
        next_progress = 0.0
        phase_index, phase = self._phase_at(0.0)

        self._log(f"entering phase {phase!r}", phase=phase)

        while True:
            elapsed = time.monotonic() - started
            self.elapsed_s = min(elapsed, self.duration_s)

            if self.should_cancel is not None and self.should_cancel():
                self.cancelled = True
                self._log(
                    f"cancelled after {elapsed:.1f}s of {self.duration_s:.0f}s "
                    f"and {self.progress_emitted} progress ticks",
                    level="WARNING",
                    phase=phase,
                )
                return
            if elapsed >= self.duration_s:
                self._log(f"completed {self.duration_s:.0f}s, {tick} progress ticks", phase=phase)
                return

            index, name = self._phase_at(elapsed)
            if index != phase_index:
                phase_index, phase = index, name
                self._log(f"entering phase {phase!r} at {elapsed:.1f}s", phase=phase)

            if elapsed >= next_log:
                self._emit_logs(elapsed, phase)
                next_log += self.log_interval_s

            if elapsed >= next_progress:
                tick += 1
                self._emit_progress(tick, elapsed, lag=elapsed - next_progress, phase=phase)
                next_progress += self.progress_interval_s

            # Sleep to whichever comes first — the next due event, the end of
            # the run, or the cancellation poll — so a long interval never
            # costs cancellation latency.
            due = min(next_log, next_progress, self.duration_s)
            time.sleep(max(0.0, min(POLL_S, due - (time.monotonic() - started))))

    # --- emission --------------------------------------------------------

    def _emit_logs(self, elapsed: float, phase: str) -> None:
        for _ in range(self.logs_per_tick):
            self.logs_emitted += 1
            n = self.logs_emitted
            level = "INFO"
            if n % WARNING_EVERY == 0:
                level = "WARNING"
            elif n % DEBUG_EVERY == 0:
                level = "DEBUG"
            visible = not (self.quiet_every and n % self.quiet_every == 0)
            self._log(
                f"heartbeat {n} at {elapsed:.1f}s / {self.duration_s:.0f}s ({phase})",
                level=level,
                phase=phase,
                visible=visible,
            )

    def _wave(self, elapsed: float) -> float:
        """Something to plot. One full cycle over the run, offset by the seed, so
        the progress chart has a recognisable shape and two runs with different
        seeds are visibly different rather than merely differently numbered.

        It measures nothing. It is in the payload rather than being
        `primary_metric` for exactly that reason — `primary_metric` is the
        lag, which is real."""
        return math.sin(2 * math.pi * (elapsed / self.duration_s) + self.seed)

    def _emit_progress(self, tick: int, elapsed: float, *, lag: float, phase: str) -> None:
        self.progress_emitted += 1
        percent = min(100.0, 100.0 * elapsed / self.duration_s)
        wave = self._wave(elapsed)
        index, _ = self._phase_at(elapsed)

        if self.emit is not None:
            self.emit(
                "progress",
                elapsed_seconds=elapsed,
                # Exact, not estimated — the one model where it can be.
                percent_complete=percent,
                primary_metric=max(0.0, lag),
                primary_metric_label="tick_lag_seconds",
                payload={
                    "tick": tick,
                    "phase": phase,
                    "phase_index": index,
                    "phase_count": len(self.phases),
                    "wave": wave,
                    "logs_emitted": self.logs_emitted,
                    "duration_planned_seconds": self.duration_s,
                    #: What `percent_complete` is a fraction OF. Named because
                    #: the spec asks every model to say so rather than leave a
                    #: bare percentage to be guessed at.
                    "percent_of": "elapsed wall clock over duration_seconds",
                },
            )


def build_model(config: dict[str, Any] | None = None) -> HeartbeatModel:
    return HeartbeatModel(config)
