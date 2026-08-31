from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from job.config import JobConfig


class FakeModel:
    """A self-driving model, the way a real one looks to the harness.

    No base class, no imports from the platform — exactly what
    docs/architecture.md describes.
    """

    results_table = "results_fake"
    preview_axes = ("t", "v")

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self.steps = int(cfg.get("steps", 5))
        self.step_sleep = float(cfg.get("step_sleep", 0.0))
        self.rows: list[dict[str, Any]] = []
        self.built = False
        self.emit = None
        self.should_cancel = None

    def build(self) -> None:
        self.built = True

    def run(self) -> None:
        for i in range(self.steps):
            if self.should_cancel():
                self.emit("log", message=f"stopping at step {i}", source="model", phase="run")
                return
            self.rows.append({"t": i, "v": i * 2.0})
            self.emit(
                "progress",
                elapsed_seconds=float(i),
                percent_complete=100.0 * (i + 1) / self.steps,
                primary_metric=float(i),
                primary_metric_label="step",
            )
            if self.step_sleep:
                time.sleep(self.step_sleep)

    def results(self) -> list[dict[str, Any]]:
        return self.rows


class BlockingModel:
    """Blocks until cancelled, polling at a known interval."""

    results_table = "results_blocking"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self.poll_s = float(cfg.get("poll_s", 0.01))
        self.timeout_s = float(cfg.get("timeout_s", 5.0))
        self.observed_at: float | None = None
        self.started = threading.Event()
        self.emit = None
        self.should_cancel = None

    def run(self) -> None:
        self.started.set()
        deadline = time.monotonic() + self.timeout_s
        while time.monotonic() < deadline:
            if self.should_cancel():
                self.observed_at = time.monotonic()
                return
            time.sleep(self.poll_s)
        raise AssertionError("cancellation was never observed")

    def results(self) -> list[dict[str, Any]]:
        return [{"partial": True}]


class ChunkedModel:
    """Emits results incrementally, several times per run."""

    results_table = "results_chunked"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self.chunks = int(cfg.get("chunks", 3))
        self.per_chunk = int(cfg.get("per_chunk", 4))
        self.cancel_after = cfg.get("cancel_after")
        self.emit = None
        self.should_cancel = None

    def run(self) -> None:
        for c in range(self.chunks):
            if self.should_cancel():
                return
            rows = [{"chunk": c, "i": i, "v": float(c * 10 + i)} for i in range(self.per_chunk)]
            self.emit("result", rows=rows, final=(c == self.chunks - 1))


@pytest.fixture
def cfg(tmp_path):
    def _make(**overrides):
        base = dict(
            run_id="run-test",
            model_spec="tests.job.conftest:FakeModel",
            model_config={},
            app_url=None,
            catalog="main",
            schema="dbx_leaning",
            flush_tick_s=0.05,
            flush_max_age_s=0.2,
        )
        base.update(overrides)
        return JobConfig(**base)

    return _make
