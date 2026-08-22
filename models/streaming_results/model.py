"""Rolling-origin backtest — the incremental-results case.

Every other model here produces results once, at the end. This one produces
them **in chunks, while still running**, and nothing else in the platform
exercises that path. If "write results whenever the model produces them" only
worked for the once-at-the-end case, this model is what exposes it.

What the harness does with repeated result emissions (confirmed against
``job/emitter.py`` and asserted in ``tests/job/test_runner.py``):

- each ``emit("result", rows=...)`` is a separate result message, and each
  gets its own ``chunk_index`` — distinct from ``seq``, which counts every
  message of every type;
- ``row_count`` is **that chunk's** count, not a running total;
- rows append to the same results table, stamped with their chunk index;
- the terminal status is written after the last chunk, and the harness does
  *not* also call ``results()`` when a model has streamed — so this model
  deliberately does not expose a results accessor.

Nothing here is worked around silently: if any of those change, the tests in
``tests/models/test_streaming_results.py`` fail loudly.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

__all__ = ["StreamingResultsModel", "build_model"]


class StreamingResultsModel:
    results_table = "results_streaming"
    preview_axes = ("origin", "abs_error")

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = dict(config or {})
        self.series: list[float] = list(cfg.get("series") or _default_series(
            n=int(cfg.get("n", 600)), seed=int(cfg.get("seed", 3))
        ))
        self.window = int(cfg.get("window", 120))
        self.step = int(cfg.get("step", 40))
        self.horizon = int(cfg.get("horizon", 12))
        self.lags = int(cfg.get("lags", 12))

        self.emit: Callable[..., Any] | None = None
        self.should_cancel: Callable[[], bool] | None = None

        self.chunks_emitted = 0
        self.rows_emitted = 0

    # --- windows ----------------------------------------------------------

    @property
    def origins(self) -> list[int]:
        """Where each backtest window starts forecasting from."""
        last = len(self.series) - self.horizon
        return list(range(self.window, last + 1, self.step))

    def _fit_predict(self, train: list[float]) -> list[float]:
        """Least-squares on lag features. Small on purpose — the platform
        value here is the chunking, not the modelling."""
        import numpy as np

        y = np.asarray(train, dtype=float)
        rows = [y[i - self.lags : i] for i in range(self.lags, len(y))]
        X = np.column_stack([np.vstack(rows), np.ones(len(rows))])
        target = y[self.lags :]
        coef, *_ = np.linalg.lstsq(X, target, rcond=None)

        window = y[-self.lags :]
        out = []
        for _ in range(self.horizon):
            nxt = float(np.dot(np.append(window, 1.0), coef))
            out.append(nxt)
            window = np.append(window[1:], nxt)
        return out

    # --- the run ----------------------------------------------------------

    def run(self) -> None:
        origins = self.origins
        total = len(origins)
        started = time.monotonic()
        self._log(f"rolling-origin backtest: {total} windows, horizon {self.horizon}",
                  phase="input")

        for index, origin in enumerate(origins):
            # Between chunks. Whatever has already been emitted stands —
            # there is nothing to finalise, because nothing was being held.
            if self.should_cancel is not None and self.should_cancel():
                self._log(f"cancelled after {index} of {total} windows")
                return

            predicted = self._fit_predict(self.series[:origin])
            actual = self.series[origin : origin + self.horizon]
            rows = [
                {
                    "origin": origin,
                    "step": step,
                    "predicted": round(p, 6),
                    "actual": round(a, 6),
                    "abs_error": round(abs(p - a), 6),
                }
                for step, (p, a) in enumerate(zip(predicted, actual, strict=False))
            ]
            mae = sum(r["abs_error"] for r in rows) / len(rows) if rows else 0.0

            self._emit_chunk(rows, final=(index == total - 1))
            self._progress(index + 1, total, time.monotonic() - started, mae, origin)

    def _emit_chunk(self, rows: list[dict[str, Any]], *, final: bool) -> None:
        if self.emit is None:
            return
        # One call per completed window. The harness assigns chunk_index and
        # counts the rows; this model supplies neither.
        self.emit("result", rows=rows, final=final)
        self.chunks_emitted += 1
        self.rows_emitted += len(rows)

    def _progress(self, done, total, elapsed, mae, origin) -> None:
        if self.emit is None:
            return
        self.emit(
            "progress",
            elapsed_seconds=elapsed,
            percent_complete=100.0 * done / total if total else 100.0,
            primary_metric=mae,
            primary_metric_label="window_mae",
            payload={"windows_done": done, "windows_total": total, "origin": origin},
        )

    def _log(self, message: str, *, phase: str = "run") -> None:
        if self.emit is not None:
            self.emit("log", message=message, source="model", phase=phase)


def _default_series(n: int = 600, *, seed: int = 3) -> list[float]:
    import numpy as np

    rng = np.random.default_rng(seed)
    t = np.arange(n)
    return (
        20.0
        + 5.0 * np.sin(2 * np.pi * t / 24)
        + 0.02 * t
        + rng.normal(0, 0.8, size=n)
    ).tolist()


def build_model(config: dict[str, Any] | None = None) -> StreamingResultsModel:
    return StreamingResultsModel(config)
