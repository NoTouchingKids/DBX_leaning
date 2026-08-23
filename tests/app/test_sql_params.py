"""Bound, *typed* parameters — the regression test for v1's real bug.

An untyped parameter is compared as a string server-side, so seq 9 sorts after
seq 10 and a cursor silently stalls. The type is not decoration.
"""

from __future__ import annotations

import pytest

from app.repository import RunRepository
from app.sql import P, SqlClient, SqlUnavailable
from shared.tables import TableSet

from .conftest import FakeHttp, statement_response


def client(http, **kw):
    return SqlClient("https://ws.example.com", "wh-1", "tok", client=http, **kw)


async def test_integer_parameters_are_declared_as_INT():
    http = FakeHttp(statement_response(["seq"], [[1]]))
    await client(http).query("SELECT 1 WHERE seq > :after_seq", [P.int("after_seq", 9)])

    params = http.requests[0]["json"]["parameters"]
    assert params == [{"name": "after_seq", "value": "9", "type": "INT"}]


async def test_a_cursor_query_binds_its_bound_as_an_integer():
    # The specific shape that broke: "2" > "12" lexicographically.
    http = FakeHttp(statement_response(["seq", "ts", "type", "body"], []))
    repo = RunRepository(client(http), TableSet())
    await repo.messages_since("run-1", after_seq=9, limit=100)

    by_name = {p["name"]: p for p in http.requests[0]["json"]["parameters"]}
    assert by_name["after_seq"]["type"] == "INT"
    assert by_name["row_limit"]["type"] == "INT"
    assert by_name["run_id"]["type"] == "STRING"


async def test_nothing_is_interpolated_into_the_sql_text():
    http = FakeHttp(statement_response(["seq", "ts", "type", "body"], []))
    repo = RunRepository(client(http), TableSet())
    await repo.messages_since("'; DROP TABLE run_logs; --", after_seq=0, limit=10)

    sql = http.requests[0]["json"]["statement"]
    assert "DROP TABLE" not in sql
    assert ":run_id" in sql and ":after_seq" in sql


async def test_the_api_does_the_waiting_rather_than_us_polling():
    http = FakeHttp(statement_response(["x"], [[1]]))
    await client(http, wait_timeout_s=30).query("SELECT 1")
    body = http.requests[0]["json"]
    assert body["wait_timeout"] == "30s"
    assert body["disposition"] == "INLINE" and body["format"] == "JSON_ARRAY"


@pytest.mark.parametrize("given,expected", [(1, "5s"), (30, "30s"), (500, "50s")])
async def test_wait_timeout_is_clamped_to_the_apis_range(given, expected):
    http = FakeHttp(statement_response(["x"], []))
    await client(http, wait_timeout_s=given).query("SELECT 1")
    assert http.requests[0]["json"]["wait_timeout"] == expected


async def test_no_warehouse_is_a_clear_error_not_an_attribute_error():
    with pytest.raises(SqlUnavailable, match="no SQL warehouse configured"):
        await SqlClient(None, None, None).query("SELECT 1")


async def test_rows_come_back_as_dicts_keyed_by_column():
    http = FakeHttp(statement_response(["run_id", "status"], [["r1", "RUNNING"]]))
    rows = await client(http).query("SELECT run_id, status FROM t")
    assert rows == [{"run_id": "r1", "status": "RUNNING"}]


async def test_backfill_rehydrates_nested_json_columns():
    body = (
        '{"elapsed_seconds": 1.5, "percent_complete": 40.0, "primary_metric": 0.03,'
        ' "primary_metric_label": "mip_gap", "payload_json": "{\\"nodes\\": 12}"}'
    )
    http = FakeHttp(statement_response(["seq", "ts", "type", "body"], [[7, 123, "progress", body]]))
    repo = RunRepository(client(http), TableSet())
    messages = await repo.messages_since("r1", 0, 10)

    assert messages[0]["payload"] == {"nodes": 12}
    assert messages[0]["seq"] == 7 and messages[0]["type"] == "progress"
    assert "payload_json" not in messages[0]


def test_a_string_parameter_stores_a_string_not_whatever_it_was_given():
    # A STRING param holding an int is the declared type and the stored value
    # disagreeing — the same class of bug typed parameters exist to prevent.
    assert P.str("job_run_id", 987654).value == "987654"
    assert P.str("detail", None).value is None
    assert P.str("job_run_id", 987654).as_api() == {
        "name": "job_run_id", "value": "987654", "type": "STRING"
    }
