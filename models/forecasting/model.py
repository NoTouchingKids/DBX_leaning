"""Time-series forecasting with a real training loop.

Its job in this platform is not forecast accuracy — it is proving the
envelope's ``progress`` shape works for **training-loop telemetry** (epochs,
train/val loss), which looks nothing like a MIP gap or a completion
percentage. If a generic progress view cannot render this model without
special-casing, the envelope itself needs revisiting.

Deliberately light: ``SGDRegressor.partial_fit`` over lag features gives a
genuine epoch boundary, early-stopping-style best-checkpoint tracking, and
trains in under a second. No deep-learning stack for a platform test.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from typing import Any

__all__ = ["ForecastingModel", "build_model", "synthetic_series"]


def synthetic_series(n: int = 720, *, seed: int = 7, period: int = 24) -> list[float]:
    """A daily-seasonal series with trend and noise. Deterministic for a seed.

    Free Edition ships sample data (the ``samples`` catalog) that would do just
    as well; this keeps the model runnable — and testable — with no workspace.
    """
    import numpy as np

    rng = np.random.default_rng(seed)
    t = np.arange(n)
    seasonal = 10.0 * np.sin(2 * np.pi * t / period)
    weekly = 4.0 * np.sin(2 * np.pi * t / (period * 7))
    trend = 0.01 * t
    noise = rng.normal(0, 1.5, size=n)
    return (50.0 + seasonal + weekly + trend + noise).tolist()


class ForecastingModel:
    results_table = "results_forecasting"
    preview_axes = ("step", "forecast")

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = dict(config or {})
        self.series: list[float] = list(cfg.get("series") or synthetic_series(
            n=int(cfg.get("n", 720)), seed=int(cfg.get("seed", 7))
        ))
        self.lags = int(cfg.get("lags", 24))
        self.horizon = int(cfg.get("horizon", 48))
        self.epochs = int(cfg.get("epochs", 40))
        self.seed = int(cfg.get("seed", 7))

        self.emit: Callable[..., Any] | None = None
        self.should_cancel: Callable[[], bool] | None = None

        self.history: list[dict[str, float]] = []
        self.best_val: float | None = None
        self._best_weights: Any = None
        self._model: Any = None
        self._scale: tuple[float, float] = (0.0, 1.0)

    # --- data -------------------------------------------------------------

    def _windows(self):
        import numpy as np

        y = np.asarray(self.series, dtype=float)
        rows = [y[i - self.lags : i] for i in range(self.lags, len(y))]
        X = np.vstack(rows)
        target = y[self.lags :]
        split = int(len(X) * 0.8)
        return X[:split], target[:split], X[split:], target[split:]

    def build(self) -> None:
        import numpy as np
        from sklearn.linear_model import SGDRegressor

        X_train, y_train, X_val, y_val = self._windows()
        mean, std = float(X_train.mean()), float(X_train.std() or 1.0)
        self._scale = (mean, std)
        self._data = (
            (X_train - mean) / std,
            (y_train - mean) / std,
            (X_val - mean) / std,
            (y_val - mean) / std,
        )
        self._raw_val = y_val
        self._model = SGDRegressor(
            learning_rate="constant", eta0=0.01, penalty="l2", alpha=1e-4,
            random_state=self.seed,
        )
        self._log(
            f"{len(X_train)} train windows, {len(X_val)} val windows, "
            f"{self.lags} lags, horizon {self.horizon}",
            phase="input",
        )
        _ = np  # numpy is a hard dependency of this track; keep the import honest

    # --- training ---------------------------------------------------------

    def run(self) -> None:
        import numpy as np

        X_train, y_train, X_val, y_val = self._data
        started = time.monotonic()

        for epoch in range(self.epochs):
            # Between epochs — the natural checkpoint boundary for this model.
            if self.should_cancel is not None and self.should_cancel():
                self._log(f"cancelled after epoch {epoch} of {self.epochs}")
                break

            self._model.partial_fit(X_train, y_train)
            train_loss = float(np.mean((self._model.predict(X_train) - y_train) ** 2))
            val_loss = float(np.mean((self._model.predict(X_val) - y_val) ** 2))
            if not math.isfinite(val_loss):
                self._log(f"epoch {epoch}: diverged, stopping", level="WARNING")
                break

            self.history.append(
                {"epoch": epoch, "train_loss": train_loss, "val_loss": val_loss}
            )
            # Early-stopping-style: a cancelled run keeps the best weights it
            # has seen, not whatever the last epoch happened to leave behind.
            if self.best_val is None or val_loss < self.best_val:
                self.best_val = val_loss
                self._best_weights = (self._model.coef_.copy(), float(self._model.intercept_[0]))

            self._progress(epoch, train_loss, val_loss, time.monotonic() - started)

    def _progress(self, epoch: int, train_loss: float, val_loss: float, elapsed: float) -> None:
        if self.emit is None:
            return
        self.emit(
            "progress",
            elapsed_seconds=elapsed,
            percent_complete=100.0 * (epoch + 1) / self.epochs,
            primary_metric=val_loss,
            primary_metric_label="val_loss",
            payload={
                "epoch": epoch,
                "epochs_total": self.epochs,
                "train_loss": train_loss,
                "best_val_loss": self.best_val,
                "learning_rate": float(self._model.eta0),
            },
        )

    # --- results ----------------------------------------------------------

    def results(self) -> list[dict[str, Any]]:
        """A recursive forecast over the horizon, from the best checkpoint.

        One row per forecasted timestep, plus the evaluation metrics on the
        held-out window — this is the model where a forecast-vs-actual preview
        matters most.
        """
        import numpy as np

        if self._model is None or self._best_weights is None:
            return []

        coef, intercept = self._best_weights
        mean, std = self._scale
        window = (np.asarray(self.series[-self.lags :], dtype=float) - mean) / std

        rows: list[dict[str, Any]] = []
        mae, rmse = self._val_metrics(coef, intercept)
        for step in range(self.horizon):
            scaled = float(np.dot(window, coef) + intercept)
            window = np.append(window[1:], scaled)
            rows.append(
                {
                    "step": step,
                    "forecast": round(scaled * std + mean, 6),
                    "val_mae": round(mae, 6),
                    "val_rmse": round(rmse, 6),
                    "epochs_trained": len(self.history),
                }
            )
        return rows

    def _val_metrics(self, coef, intercept) -> tuple[float, float]:
        import numpy as np

        _, _, X_val, _ = self._data
        mean, std = self._scale
        predicted = (X_val @ coef + intercept) * std + mean
        error = predicted - self._raw_val
        return float(np.mean(np.abs(error))), float(math.sqrt(np.mean(error**2)))

    def _log(self, message: str, *, phase: str = "run", level: str = "INFO") -> None:
        if self.emit is not None:
            self.emit("log", message=message, level=level, source="model", phase=phase)


def build_model(config: dict[str, Any] | None = None) -> ForecastingModel:
    return ForecastingModel(config)
