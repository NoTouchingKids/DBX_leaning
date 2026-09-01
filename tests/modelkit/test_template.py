"""The template's own behaviour, tested once so no model has to test it again.

That is the argument for the template in miniature: cancel handling,
percentage arithmetic and an interruptible sleep are things every model needs
and none should get subtly wrong on its own. They are checked here, and a
model inherits both the behaviour and the guarantee.

Nothing in this file imports the platform. `modelkit` is stdlib-only by
design, so its tests are too.
"""

from __future__ import annotations

import time

import pytest
from modelkit import CANCELLED, STOP, SUCCEEDED, Model


class Counter(Model):
    """The smallest possible model: the required method and nothing else."""

    unit = "counts"

    def configs(self):
        return {"n": 3}

    def prestep(self):
        self.total = self.n

    def step(self, i):
        return None


def drive(model: Model, *, cancel_after: int | None = None) -> tuple[str, list[tuple]]:
    """Run a model the way the harness does, collecting what it emitted."""
    seen: list[tuple] = []
    calls = {"n": 0}

    def should_cancel() -> bool:
        calls["n"] += 1
        return cancel_after is not None and calls["n"] > cancel_after

    model.attach(
        emit=lambda type, **fields: seen.append((type, fields)),
        should_cancel=should_cancel,
    )
    return model.run(), seen


def of_type(seen: list[tuple], type_: str) -> list[dict]:
    return [fields for t, fields in seen if t == type_]


# --- configuration ---------------------------------------------------------


def test_configs_become_attributes():
    """`configs()` returning `{"n": 3}` gives you `self.n`. That is the whole
    mechanism — no schema, no descriptor, no declaration to keep in step."""
    assert Counter().n == 3
    assert Counter({"n": 9}).n == 9
    assert Counter(n=9).n == 9


def test_both_call_shapes_agree():
    """A dict is what the harness passes from DBX_MODEL_CONFIG; keywords are
    what a person types in a notebook. Neither caller should have to know the
    other exists."""
    assert Counter({"n": 4}).config == Counter(n=4).config == {"n": 4}


def test_keywords_win_over_a_config_dict():
    """Most specific wins, which is what a notebook overriding one field of a
    job's configuration expects."""
    assert Counter({"n": 4}, n=5).n == 5


def test_a_config_key_that_would_shadow_a_method_is_refused():
    """The failure this prevents is remote from its cause: `self.step` becomes
    an int, and the traceback is about an int not being callable, thrown from
    inside the run loop rather than from the configuration that broke it."""
    with pytest.raises(TypeError, match="would shadow"):
        Counter(step=1)


def test_config_is_kept_whole():
    """Flattening defaults and overrides into attributes loses which was
    which; `prestep` frequently wants to report what it was given."""
    assert Counter({"n": 4}).config == {"n": 4}


# --- the loop --------------------------------------------------------------


def test_a_plain_run_reaches_succeeded_and_reports_progress():
    status, seen = drive(Counter(n=4))

    assert status == SUCCEEDED
    assert len(of_type(seen, "progress")) == 4
    # A start log and a finish log, so a run is legible with no progress view.
    assert len(of_type(seen, "log")) == 2


def test_progress_carries_a_percentage_when_the_total_is_known():
    _, seen = drive(Counter(n=4))
    percents = [p["percent_complete"] for p in of_type(seen, "progress")]
    assert percents == [25.0, 50.0, 75.0, 100.0]


def test_progress_carries_no_percentage_when_the_total_is_unknown():
    """A made-up percentage is worse than an absent one: it is the number a
    progress bar trusts, and there is nothing to compute it from."""

    class Unbounded(Model):
        def step(self, i):
            return STOP if i >= 2 else None

    status, seen = drive(Unbounded())
    assert status == SUCCEEDED
    assert all(p["percent_complete"] is None for p in of_type(seen, "progress"))


def test_the_step_count_is_the_default_metric():
    _, seen = drive(Counter(n=3))
    progress = of_type(seen, "progress")
    assert [p["primary_metric"] for p in progress] == [1.0, 2.0, 3.0]
    assert {p["primary_metric_label"] for p in progress} == {"counts"}


def test_a_step_can_name_its_own_metric_and_keep_the_rest_as_payload():
    """`metric` and `label` are lifted out because every model wants them;
    everything else travels as payload for a model-specific view."""

    class Solver(Model):
        total = 1

        def step(self, i):
            return {"metric": 0.25, "label": "gap", "nodes": 17}

    _, seen = drive(Solver())
    (progress,) = of_type(seen, "progress")

    assert progress["primary_metric"] == 0.25
    assert progress["primary_metric_label"] == "gap"
    assert progress["payload"] == {"nodes": 17}


def test_stop_ends_the_loop_without_cancelling_it():
    """A solver that converges early has SUCCEEDED, not been interrupted.
    Conflating the two would misreport every model that converges."""

    class Converges(Model):
        total = 100

        def step(self, i):
            return STOP if i == 3 else None

    status, seen = drive(Converges())
    assert status == SUCCEEDED
    assert len(of_type(seen, "progress")) == 3


# --- cancellation ----------------------------------------------------------


def test_a_cancel_stops_the_loop_and_returns_cancelled():
    status, seen = drive(Counter(n=1000), cancel_after=3)

    assert status == CANCELLED
    assert len(of_type(seen, "progress")) < 10, "the loop ran on well past the cancel"
    assert any(log["level"] == "WARNING" for log in of_type(seen, "log"))


def test_a_cancel_during_the_wait_is_noticed_within_a_tenth_of_a_second():
    """The reason `sleep` is sliced. A model with a thirty-second interval that
    only checked between steps would take thirty seconds to honour a cancel."""

    class Slow(Model):
        total = 10
        interval = 5.0

        def step(self, i):
            return None

    started = time.monotonic()
    status, _ = drive(Slow(), cancel_after=2)
    elapsed = time.monotonic() - started

    assert status == CANCELLED
    assert elapsed < 1.0, f"took {elapsed:.2f}s to notice a cancel during a 5s wait"


# --- lifecycle -------------------------------------------------------------


def test_prestep_runs_inside_the_run_not_at_construction():
    """So that what it emits belongs to the run, and what it raises fails the
    run rather than the constructor. A model that cannot read its data has
    failed a run — the harness records that; a constructor cannot."""
    order: list[str] = []

    class Ordered(Model):
        total = 1

        def prestep(self):
            order.append("prestep")

        def step(self, i):
            order.append("step")

        def poststep(self, status):
            order.append(f"poststep:{status}")

    model = Ordered()
    assert order == [], "prestep ran at construction"

    drive(model)
    assert order == ["prestep", "step", "poststep:SUCCEEDED"]


def test_poststep_runs_on_the_cancelled_path():
    """A cancelled run keeps its incumbent, so the hook that writes results has
    to run when it is cancelled — otherwise cancelling discards the work."""
    seen: list[str] = []

    class Keeps(Model):
        total = 1000

        def step(self, i):
            return None

        def poststep(self, status):
            seen.append(status)

    drive(Keeps(), cancel_after=2)
    assert seen == [CANCELLED]


def test_poststep_runs_when_a_step_raises_and_the_exception_still_propagates():
    """Both halves matter. The model gets to keep what it has; the harness
    still sees the exception and records a FAILED run with the traceback."""
    seen: list[str] = []

    class Breaks(Model):
        total = 3

        def step(self, i):
            raise ValueError("boom")

        def poststep(self, status):
            seen.append(status)

    with pytest.raises(ValueError, match="boom"):
        drive(Breaks())
    assert seen == ["FAILED"]


# --- the contract with the platform ----------------------------------------


def test_a_model_runs_with_nothing_attached():
    """`attach` is the harness's to call, and a model must work without it —
    that is what makes one runnable in a REPL before any of this exists."""
    assert Counter(n=2).run() == SUCCEEDED


def test_the_template_imports_no_platform_code():
    """`modelkit` is stdlib-only, and that is what lets it be installed into
    every model environment for free. An import of `shared` or `job` here
    would put the whole platform in each one."""
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, json, modelkit\n"
            "third = sorted(m for m in sys.modules\n"
            "               if m.partition('.')[0] not in sys.stdlib_module_names\n"
            "               and not m.startswith('_'))\n"
            "print(json.dumps(third))",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith('["modelkit", "modelkit.base"]'), result.stdout
