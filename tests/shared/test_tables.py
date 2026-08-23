import json

from shared.envelope import make_message
from shared.tables import EVENTS, LOGS, PROGRESS, RESULT_META, TableSet, table_for, to_row


def test_each_message_type_routes_to_its_own_table():
    assert table_for(make_message("log", run_id="r", seq=0, message="x")) == LOGS
    assert table_for(make_message("progress", run_id="r", seq=0, elapsed_seconds=0.0)) == PROGRESS
    assert table_for(make_message("status", run_id="r", seq=0, status="RUNNING")) == EVENTS
    assert table_for(make_message("result", run_id="r", seq=0, row_count=0)) == RESULT_META


def test_qualification_leaves_already_qualified_names_alone():
    ts = TableSet(catalog="main", schema="dbx_leaning")
    assert ts.qualify("run_logs") == "main.dbx_leaning.run_logs"
    assert ts.qualify("other.place.results") == "other.place.results"


def test_rows_are_flat_and_json_serialisable():
    m = make_message(
        "progress",
        run_id="r",
        seq=7,
        elapsed_seconds=1.0,
        payload={"nodes": 12, "incumbent": None},
    )
    row = to_row(m)
    assert row["run_id"] == "r" and row["seq"] == 7
    assert json.loads(row["payload_json"]) == {"nodes": 12, "incumbent": None}
    assert all(not isinstance(v, (dict, list)) for v in row.values())


def test_client_invisible_logs_are_still_written_durably():
    row = to_row(make_message("log", run_id="r", seq=0, message="raw", client_visible=False))
    assert row["message"] == "raw"
    assert row["client_visible"] is False
