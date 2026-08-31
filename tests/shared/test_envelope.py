"""The envelope's shape is the contract. These tests are what freezes it."""

import math

import pytest
from pydantic import ValidationError

from shared.envelope import (
    PREVIEW_MAX_POINTS,
    TERMINAL_STATUSES,
    LogLevel,
    LogMessage,
    MessageAdapter,
    MessageType,
    ProgressMessage,
    ResultMessage,
    RunStatus,
    StatusMessage,
    make_message,
    now_ms,
    sanitize_metric,
)


def test_every_message_type_carries_the_common_fields():
    built = [
        make_message("log", run_id="r", seq=0, message="x"),
        make_message("progress", run_id="r", seq=1, elapsed_seconds=0.0),
        make_message("status", run_id="r", seq=2, status="RUNNING"),
        make_message("result", run_id="r", seq=3, row_count=0),
    ]
    for m in built:
        assert m.run_id == "r"
        assert isinstance(m.seq, int)
        assert m.ts > 0


def test_type_is_the_discriminator_on_the_way_back_in():
    for payload, expected in [
        ({"type": "log", "message": "x"}, LogMessage),
        ({"type": "progress", "elapsed_seconds": 1.0}, ProgressMessage),
        ({"type": "status", "status": "QUEUED"}, StatusMessage),
        ({"type": "result", "row_count": 5}, ResultMessage),
    ]:
        m = MessageAdapter.validate_python({"run_id": "r", "seq": 1, "ts": now_ms(), **payload})
        assert isinstance(m, expected)


def test_unknown_type_names_the_valid_ones():
    with pytest.raises(ValueError, match="log, progress, status, result"):
        make_message("telemetry", run_id="r", seq=0)


def test_messages_are_frozen():
    m = make_message("log", run_id="r", seq=0, message="x")
    with pytest.raises(ValidationError):
        m.seq = 99


def test_unknown_fields_are_rejected_not_silently_dropped():
    # A model inventing its own field is the drift this contract exists to
    # prevent; it should fail loudly at the boundary.
    with pytest.raises(ValidationError):
        make_message("log", run_id="r", seq=0, message="x", severity="high")


def test_seq_and_ts_cannot_go_negative():
    with pytest.raises(ValidationError):
        make_message("log", run_id="r", seq=-1, message="x")
    with pytest.raises(ValidationError):
        make_message("log", run_id="r", seq=0, ts=-1, message="x")


def test_log_defaults_are_client_visible_info():
    m = make_message("log", run_id="r", seq=0, message="x")
    assert m.level is LogLevel.INFO
    assert m.client_visible is True


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_primary_metric_becomes_null(bad):
    m = make_message("progress", run_id="r", seq=0, elapsed_seconds=1.0, primary_metric=bad)
    assert m.primary_metric is None


def test_finite_sentinels_are_not_this_layers_job():
    # Gurobi's pre-incumbent ±1e100 is finite, so it passes here by design —
    # it is handled where it is produced, inside the model.
    assert sanitize_metric(1e100) == 1e100
    assert sanitize_metric(None) is None
    assert sanitize_metric(math.nan) is None


def test_percent_complete_is_bounded_and_nullable():
    assert make_message("progress", run_id="r", seq=0, elapsed_seconds=1.0).percent_complete is None
    with pytest.raises(ValidationError):
        make_message("progress", run_id="r", seq=0, elapsed_seconds=1.0, percent_complete=101)


def test_result_row_count_is_required_and_may_be_zero():
    # "succeeded, wrote 0 rows" must be expressible and distinguishable.
    assert make_message("result", run_id="r", seq=0, row_count=0).row_count == 0
    with pytest.raises(ValidationError):
        make_message("result", run_id="r", seq=0)


def test_oversized_preview_is_refused_rather_than_truncated():
    rows = [{"i": i} for i in range(PREVIEW_MAX_POINTS + 1)]
    with pytest.raises(ValidationError, match="downsample"):
        make_message("result", run_id="r", seq=0, row_count=1, preview=rows)


def test_result_chunking_defaults_to_the_once_at_the_end_case():
    m = make_message("result", run_id="r", seq=0, row_count=3)
    assert m.chunk_index == 0 and m.final is True


def test_terminal_statuses_are_exactly_the_finished_ones():
    assert TERMINAL_STATUSES == {
        RunStatus.SUCCEEDED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
        RunStatus.INFEASIBLE,
    }
    assert RunStatus.RUNNING not in TERMINAL_STATUSES


def test_message_type_enum_covers_every_built_message():
    assert {m.value for m in MessageType} == {"log", "progress", "status", "result"}
