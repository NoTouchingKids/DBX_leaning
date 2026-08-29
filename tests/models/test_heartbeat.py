"""The model that exists to be watched, not to compute.

Two things this suite defends that no other model's does:

1. **It stops when told, quickly.** Cancellation is the reason this model
   exists in the shape it does — a run nobody can interrupt is not a test of
   the cancel path, it is a ten-minute wait. So the latency is asserted with a
   clock, not left to POLL_S being a small-looking constant.
2. **It runs for the time it was asked to, and not longer.** Every other
   model's runtime is a consequence of its work. This one's is a number a
   person typed, which is what makes it useful and what makes a bad number
   expensive: a run holds one of five account-wide task slots for its whole
   duration.
3. **It produces no results, on purpose.** The only model in the lineup with
   no `results_table` and no `results()`. What it exercises is the live path,
   and a results table would give it a Delta write and a Unity Catalog
   dependency to fail on — a diagnostic that can fail for reasons unrelated to
   what it diagnoses is worse than no diagnostic.

Everything here runs in well under a second of wall clock except the two
timing tests, which are bounded and say so.
"""

from __future__ import annotations

import ast
import pathlib
import time

import pytest

from job.models.heartbeat import build_model
from job.models.heartbeat import model as heartbeat_model

PACKAGE = pathlib.Path(heartbeat_model.__file__).parent

#: Sub-second, with both timers firing several times. Fast enough to run in
#: every suite, long enough that phase changes and lag are real.
FAST = {
    "duration_seconds": 0.4,
    "log_interval_seconds": 0.03,
    "progress_interval_seconds": 0.05,
}


def run(recorder, **config):
    rec = recorder()
    model = rec.attach(build_model({**FAST, **config}))
    model.build()
    model.run()
    return rec, model


# --- the reason it exists --------------------------------------------------


def test_it_stops_within_the_poll_interval_of_being_cancelled(recorder):
    """The headline property. A ten-minute run must abandon in a fraction of a
    second, or the cancel button in the UI reads as broken.

    Timing test, deliberately: POLL_S being 0.25 in the source proves nothing
    about how long the loop actually takes to notice. The budget is generous
    against POLL_S so this does not go flaky on a loaded machine, and still far
    below the 600s the run was asked for.
    """
    rec = recorder()
    model = rec.attach(build_model({"duration_seconds": 600}))
    model.build()
    rec.cancel()

    started = time.monotonic()
    model.run()
    took = time.monotonic() - started

    assert took < 2.0, f"took {took:.2f}s to notice a cancel"
    assert model.cancelled is True


def test_a_cancelled_run_keeps_the_ticks_it_already_streamed(recorder):
    """A cancel truncates the stream; it does not retract it. Everything
    already emitted stands, and `elapsed_s` records where it stopped."""
    rec = recorder(cancel_after=12)
    model = rec.attach(build_model({**FAST, "duration_seconds": 30}))
    model.build()
    model.run()

    assert model.cancelled is True
    assert model.elapsed_s < model.duration_s, "a cancel must stop the clock early"
    assert model.progress_emitted > 0, "the ticks it managed are the record of the run"


def test_it_runs_for_about_as_long_as_it_was_asked_to(recorder):
    """Timing test, bounded. The floor is what matters — a model that returns
    early is not soaking anything — and the ceiling catches a loop that
    oversleeps past its own deadline."""
    started = time.monotonic()
    _, model = run(recorder, duration_seconds=0.8)
    took = time.monotonic() - started

    assert 0.8 <= took < 1.8, f"asked for 0.8s, took {took:.2f}s"
    assert model.cancelled is False


def test_a_completed_run_says_so_and_a_cancelled_one_does_not(recorder):
    rec, model = run(recorder)
    assert model.cancelled is False
    assert any("completed" in m["message"] for m in rec.of("log"))


# --- the telemetry it is built to produce ----------------------------------


def test_every_message_is_a_legal_envelope(recorder):
    rec, _ = run(recorder)
    rec.validate_all()


def test_it_emits_both_streams_on_their_own_schedules(recorder):
    """Independent intervals are the whole configuration surface — logs at one
    rate, progress at another. A shared timer would make the two indivisible
    and the model useless for loading one path without the other."""
    rec, _ = run(recorder, log_interval_seconds=0.02, progress_interval_seconds=0.2)

    logs, progress = rec.of("log"), rec.of("progress")
    assert len(logs) > len(progress) * 2, (
        f"{len(logs)} logs against {len(progress)} progress ticks — the two "
        "intervals are not being honoured separately"
    )


def test_percent_complete_is_exact_and_names_what_it_is_a_fraction_of(recorder):
    """The one model where the number is not an estimate. It still has to say
    what it measures, because a bare percentage is not readable on its own."""
    rec, _ = run(recorder)
    progress = rec.of("progress")

    for message in progress:
        expected = 100.0 * message["elapsed_seconds"] / FAST["duration_seconds"]
        assert message["percent_complete"] == pytest.approx(min(100.0, expected))
        assert "elapsed wall clock" in message["payload"]["percent_of"]

    assert progress[-1]["percent_complete"] > progress[0]["percent_complete"]


def test_the_headline_metric_is_the_models_own_lateness(recorder):
    """There is no objective to report, so `primary_metric` is scheduling lag —
    a real measurement of a soak run rather than a placeholder."""
    rec, _ = run(recorder)

    for message in rec.of("progress"):
        assert message["primary_metric_label"] == "tick_lag_seconds"
        assert message["primary_metric"] >= 0.0


def test_a_late_tick_does_not_push_the_whole_schedule_back(recorder):
    """Both timers step from the run's START, not from the last tick.

    Advancing from "now" would let one slow tick delay every later one, and the
    lag would then read as zero forever after — the drift having been absorbed
    into the schedule instead of measured.
    """
    rec, _ = run(recorder, duration_seconds=0.6, progress_interval_seconds=0.1)
    elapsed = [m["elapsed_seconds"] for m in rec.of("progress")]

    for i, seen in enumerate(elapsed):
        # Each tick lands at or after its slot and before the next one, which
        # is only true if slots are fixed rather than chained.
        assert i * 0.1 <= seen < (i + 2) * 0.1, f"tick {i} at {seen:.3f}s"


def test_it_moves_through_every_phase_in_order_and_logs_each_change(recorder):
    """Named stages exist so something *changes* during a run — a client's
    grouping and filtering need transitions to be tested against."""
    rec, model = run(recorder)

    phases = [m["payload"]["phase"] for m in rec.of("progress")]
    assert phases[0] == "warmup"
    assert phases[-1] == "cooldown"
    # Monotonic: never back to an earlier phase.
    indexes = [m["payload"]["phase_index"] for m in rec.of("progress")]
    assert indexes == sorted(indexes)
    assert set(phases) == set(model.phases)

    entered = [m["message"] for m in rec.of("log") if "entering phase" in m["message"]]
    assert len(entered) == len(model.phases)


def test_custom_phases_replace_the_defaults(recorder):
    rec, model = run(recorder, phases=["alpha", "omega"])
    assert model.phases == ("alpha", "omega")
    assert {m["payload"]["phase"] for m in rec.of("progress")} <= {"alpha", "omega"}


def test_log_levels_vary_so_a_level_filter_has_something_to_filter(recorder):
    """Deterministic, not random: a test can say which lines will be WARNING,
    and so can somebody reading the stream."""
    rec, _ = run(recorder, duration_seconds=0.5, log_interval_seconds=0.005, logs_per_tick=10)

    levels = {m.get("level", "INFO") for m in rec.of("log")}
    assert {"INFO", "DEBUG", "WARNING"} <= levels


def test_logs_per_tick_bursts_the_bus(recorder):
    """The knob for loading the live path: same schedule, many more messages.
    Dropping under pressure is a contract the bus has, and this is what makes
    it reachable on demand."""
    single, _ = run(recorder, logs_per_tick=1)
    burst, _ = run(recorder, logs_per_tick=8)

    assert len(burst.of("log")) > len(single.of("log")) * 4


def test_quiet_every_withholds_logs_from_the_live_stream_only(recorder):
    """`client_visible=False` is written durably and not sent to the browser.
    Nothing else in the lineup emits one, so nothing else can exercise the
    backfill's obligation to honour it."""
    rec, _ = run(recorder, duration_seconds=0.5, log_interval_seconds=0.01, quiet_every=3)

    visibility = [m.get("client_visible", True) for m in rec.of("log")]
    assert False in visibility and True in visibility


def test_by_default_every_log_reaches_the_client(recorder):
    """The default run is for watching, so it withholds nothing."""
    rec, _ = run(recorder)
    assert all(m.get("client_visible", True) for m in rec.of("log"))


def test_it_never_emits_status_itself(recorder):
    """Status is the harness's to emit. A model that sends its own would race
    the harness's terminal transition."""
    rec, _ = run(recorder)
    assert rec.of("status") == []


# --- what it deliberately does NOT do --------------------------------------


def test_it_declares_no_results_table_and_no_results_accessor():
    """The exemption in `tests/deploy/test_bundle.py` is a claim about this
    model; this is the model's own half of it.

    `job/runner.py::_collect_results` emits no `result` message at all for a
    model exposing neither, which is what makes "no Delta write, no UC
    dependency" true rather than merely intended.
    """
    model = build_model({})

    assert not hasattr(model, "results_table")
    assert not hasattr(model, "results")
    assert not hasattr(model, "get_results")
    assert not hasattr(model, "result_rows")


def test_it_emits_only_the_two_live_message_types(recorder):
    """Logs and progress, and nothing else. No result — there is none to send.
    No status — that is the harness's, and a model emitting its own would race
    the harness's terminal transition."""
    rec, _ = run(recorder)

    assert {kind for kind, _ in rec.messages} == {"log", "progress"}


def test_a_cancelled_run_emits_no_results_either(recorder):
    """A cancelled run keeping its incumbent is the rule everywhere else here.
    It does not apply to a model with no incumbent, and the absence has to be
    deliberate rather than a path that was missed."""
    rec = recorder(cancel_after=12)
    model = rec.attach(build_model({**FAST, "duration_seconds": 30}))
    model.build()
    model.run()

    assert model.cancelled is True
    assert rec.of("result") == []
    # The run still says what it did before it stopped — that record is the
    # log stream and the progress ticks, not a table.
    assert rec.of("progress")
    assert any("cancelled after" in m["message"] for m in rec.of("log"))


# --- config, and the ways it can be wrong ----------------------------------


def test_the_duration_is_clamped_rather_than_trusted(recorder):
    """A typo here is expensive in a way it is nowhere else: this run holds one
    of five account-wide task slots for as long as it lasts. Clamped, not
    rejected — the run still does what was meant, one warning louder."""
    rec = recorder()
    model = rec.attach(build_model({"duration_seconds": 999_999}))
    model.build()

    assert model.duration_s == heartbeat_model.MAX_DURATION_S
    warnings = [m for m in rec.of("log") if m.get("level") == "WARNING"]
    assert any("clamped" in m["message"] for m in warnings)


def test_a_duration_under_the_ceiling_is_left_alone(recorder):
    rec = recorder()
    model = rec.attach(build_model({"duration_seconds": 120}))
    model.build()

    assert model.duration_s == 120
    assert not [m for m in rec.of("log") if "clamped" in m["message"]]


@pytest.mark.parametrize("key", ["log_interval_seconds", "progress_interval_seconds"])
@pytest.mark.parametrize("value", [0, -1, "soon", None, float("inf")])
def test_an_interval_that_is_not_a_positive_number_fails_in_build(recorder, key, value):
    """Rejected, not clamped, and the asymmetry with duration is the point: an
    interval of 0 is not a run that went wrong, it is a loop with no meaning.
    Failing in `build()` costs nothing; failing later costs a task slot."""
    rec = recorder()
    model = rec.attach(build_model({**FAST, key: value}))

    with pytest.raises(ValueError, match=key):
        model.build()


@pytest.mark.parametrize("value", [0, -5, "later"])
def test_a_duration_that_is_not_a_positive_number_fails_in_build(recorder, value):
    rec = recorder()
    model = rec.attach(build_model({"duration_seconds": value}))

    with pytest.raises(ValueError, match="duration_seconds"):
        model.build()


def test_the_defaults_are_a_ten_minute_run(recorder):
    """The number in the docstring and the number in the code are the same
    number, asserted rather than trusted to stay in step."""
    model = build_model({})
    model.emit = lambda *a, **k: None
    model.build()

    assert model.duration_s == 600.0
    assert model.phases == heartbeat_model.DEFAULT_PHASES


# --- the claim the deployment rests on -------------------------------------


def test_the_package_imports_nothing_outside_the_standard_library():
    """Its extra is empty, like `annealing`'s. Unlike `annealing` it does not
    even read `models._data` — there is no dataset. An import added in a hurry
    is invisible in review and would put this model in an environment it says
    it does not need.
    """
    allowed = {"math", "time", "collections", "collections.abc", "typing", "__future__"}
    for path in sorted(PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""] if node.level == 0 else []
            else:
                continue
            for name in names:
                assert name.split(".")[0] in {a.split(".")[0] for a in allowed}, (
                    f"{path.name} imports {name!r}; this model's environment carries "
                    "only the harness's own transport"
                )
