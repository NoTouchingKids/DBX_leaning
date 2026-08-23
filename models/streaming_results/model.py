"""Rolling-origin backtest over real hourly demand — the incremental-results case.

Every other model here produces results once, at the end. This one produces
them **in chunks, while still running**, and nothing else in the platform
exercises that path. If "write results whenever the model produces them" only
worked for the once-at-the-end case, this model is what exposes it.

What the harness does with repeated result emissions (confirmed against
``job/emitter.py`` and asserted in ``tests/job/test_runner.py``):

- each ``emit("result", rows=...)`` is a separate result message, and
  ``Emitter._absorb_result_rows`` assigns it its own ``chunk_index`` —
  distinct from ``seq``, which counts every message of every type;
- ``row_count`` is **that chunk's** count, not a running total, and the
  emitter *rejects* a model that declares its own ``row_count``;
- rows append to the same results table, stamped with ``run_id`` and their
  chunk index;
- the terminal status is written after the last chunk
  (``JobRunner._finalise`` runs after the blocking call returns), and
  ``JobRunner._collect_results`` skips the model's results accessor entirely
  once ``result_chunks > 0`` — so this model deliberately does not expose
  one, rather than relying on that skip.

Nothing here is worked around silently: if any of those change, the tests in
``tests/models/test_streaming_results.py`` fail loudly.

## The data

The series backtested is real hourly NYC taxi trip volume from Databricks'
free ``samples`` catalog, via the shared loader in ``models/_data``. Off a
workspace — which is how the tests run, and how a contributor works — the
loader falls back to a deterministic synthetic demand curve of the same
shape. That fallback is *not* a silent one: the provenance is logged at the
``input`` phase and every result row carries ``data_source`` /
``data_synthetic`` / ``data_rows`` / ``data_fallback_reason``, so a run on
real trips and a run that fell back are distinguishable long afterwards, from
the results table alone.

A caller can still hand over its own series with ``config["series"]``.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from typing import Any

from models._data import Dataset, nyc_taxi_hourly

__all__ = ["StreamingResultsModel", "build_model"]

#: Which column of the hourly dataset is backtested unless told otherwise.
DEFAULT_COLUMN = "trips"

#: How much history to ask the loader for. The real table is smaller than
#: this, which is fine — the loader returns what exists.
DEFAULT_DAYS = 60


class StreamingResultsModel:
    results_table = "results_streaming"
    preview_axes = ("origin", "abs_error")

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = dict(config or {})
        # A caller-supplied series wins over the sample data; everything else
        # is how to ask the loader for it.
        self._series_config: Sequence[float] | None = cfg.get("series")
        self.days = int(cfg.get("days", DEFAULT_DAYS))
        self.column = str(cfg.get("column", DEFAULT_COLUMN))
        self.seed = int(cfg.get("seed", 7))
        #: Optional cap on how many observations to backtest over.
        self.limit: int | None = int(cfg["n"]) if cfg.get("n") is not None else None

        self.window = int(cfg.get("window", 120))
        self.step = int(cfg.get("step", 40))
        self.horizon = int(cfg.get("horizon", 12))
        # A full day of lags: the series is hourly and its strongest signal is
        # the daily cycle, which a 12-lag model cannot see.
        self.lags = int(cfg.get("lags", 24))

        self.emit: Callable[..., Any] | None = None
        self.should_cancel: Callable[[], bool] | None = None

        self.data: Dataset | None = None
        self.series: list[float] = []
        self._provenance: dict[str, Any] = {}

        self.chunks_emitted = 0
        self.rows_emitted = 0

    # --- input ------------------------------------------------------------

    def build(self) -> None:
        """Load the series. Separate from ``run`` so the load happens with
        ``emit`` already wired — the provenance line is the first thing a run
        says. ``run`` calls it too, so the model still works standalone."""
        self._ensure_data()

    def _load(self) -> Dataset:
        if self._series_config is not None:
            rows = [{self.column: float(value)} for value in self._series_config]
            return Dataset(rows=rows, source="config:series", synthetic=False)
        return nyc_taxi_hourly(days=self.days, seed=self.seed)

    def _ensure_data(self) -> None:
        if self.data is not None:
            return
        data = self._load()
        series = data.floats(self.column)
        if self.limit is not None:
            series = series[: self.limit]

        self.data = data
        self.series = series
        # `data_fallback_reason` is always present, even when null, so the
        # results table's schema does not depend on whether a given run
        # happened to fall back.
        self._provenance = data.describe()
        self._log(
            f"backtest input: {data.provenance}; "
            f"using {len(series)} points of '{self.column}'",
            phase="input",
        )

    # --- windows ----------------------------------------------------------

    @property
    def origins(self) -> list[int]:
        """Where each backtest window starts forecasting from."""
        self._ensure_data()
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
        self._ensure_data()
        origins = self.origins
        total = len(origins)
        started = time.monotonic()
        self._log(f"rolling-origin backtest: {total} windows, horizon {self.horizon}",
                  phase="input")

        if total == 0:
            # Nothing to backtest — a short real table should not produce a
            # run with no `final=true` message at all.
            self._log(
                f"series of {len(self.series)} points is shorter than a "
                f"{self.window}-point window plus a {self.horizon}-step horizon; "
                "no windows to backtest",
                level="WARNING",
            )
            self._emit_chunk([], final=True)
            return

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
                    # Provenance travels with the rows, not just the log: the
                    # results table has to stand on its own afterwards.
                    **self._provenance,
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
            payload={
                "windows_done": done,
                "windows_total": total,
                "origin": origin,
                **self._provenance,
            },
        )

    def _log(self, message: str, *, phase: str = "run", level: str = "INFO") -> None:
        if self.emit is not None:
            self.emit("log", message=message, source="model", phase=phase, level=level)


def build_model(config: dict[str, Any] | None = None) -> StreamingResultsModel:
    return StreamingResultsModel(config)
