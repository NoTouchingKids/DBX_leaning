"""Reading a model's own results — the half of `fetch_hint` that was missing.

The envelope carries a bounded preview and a pointer, never the rows. A
browser cannot query Unity Catalog, so without this endpoint the pointer
points nowhere.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from server.repository import RunRepository, UnsafeTableName, validate_table_name
from server.sql import SqlClient

from .conftest import FakeHttp, statement_response


class ScriptedSql:
    available = True

    def __init__(self, answers: dict[str, list[dict]] | None = None):
        self.answers = answers or {}
        self.queries: list[tuple[str, list]] = []

    async def query(self, sql, params=None):
        self.queries.append((sql, params or []))
        for fragment, rows in self.answers.items():
            if fragment in sql:
                return rows
        return []

    async def close(self): ...


def hint(table: str) -> list[dict]:
    return [{"fetch_hint_json": json.dumps({"table": table, "key": "run_id"})}]


def wire(hub, answers):
    sql = ScriptedSql(answers)
    hub.sql = sql
    hub.repo = RunRepository(sql, hub.tables)
    return sql


def test_results_come_back_from_the_table_the_job_recorded(app_and_hub):
    app, hub = app_and_hub()
    sql = wire(
        hub,
        {
            "fetch_hint_json": hint("main.dbx_leaning.results_scenario"),
            "SELECT * FROM": [{"run_id": "r1", "chunk_index": 0, "objective": 12.5}],
        },
    )

    with TestClient(app) as client:
        body = client.get("/api/runs/r1/results").json()

    assert body["table"] == "main.dbx_leaning.results_scenario"
    assert body["rows"] == [{"run_id": "r1", "chunk_index": 0, "objective": 12.5}]
    assert body["count"] == 1 and body["more"] is False

    read = next(s for s, _ in sql.queries if "SELECT * FROM" in s)
    assert "main.dbx_leaning.results_scenario" in read
    assert ":run_id" in read, "the run id must still be a bound parameter"


def test_the_table_is_read_from_fetch_hint_not_guessed_from_the_model(app_and_hub):
    """A run triggered with an overridden results table must still be readable."""
    app, hub = app_and_hub()
    wire(
        hub,
        {
            "fetch_hint_json": hint("main.dbx_leaning.results_override"),
            "SELECT * FROM": [{"run_id": "r1", "chunk_index": 0}],
        },
    )
    with TestClient(app) as client:
        assert client.get("/api/runs/r1/results").json()["table"].endswith("results_override")


def test_a_run_with_no_results_yet_is_a_404_not_an_empty_page(app_and_hub):
    app, hub = app_and_hub()
    wire(hub, {})
    with TestClient(app) as client:
        resp = client.get("/api/runs/r1/results")
    assert resp.status_code == 404
    assert "may not have reached results yet" in resp.json()["detail"]


def test_paging_reports_where_to_continue(app_and_hub):
    app, hub = app_and_hub()
    wire(
        hub,
        {
            "fetch_hint_json": hint("main.dbx_leaning.results_scenario"),
            "SELECT * FROM": [{"i": i} for i in range(5)],
        },
    )
    with TestClient(app) as client:
        body = client.get("/api/runs/r1/results", params={"limit": 5, "offset": 10}).json()

    assert body["more"] is True and body["next_offset"] == 15


def test_results_are_ordered_by_chunk_so_a_chunked_model_reads_back_in_order(app_and_hub):
    """`panel_fit` writes a chunk every `chunk_size` groups, so its table holds
    several chunks per run. Without the ORDER BY they come back in whatever
    order Delta hands them out, and a paged read would interleave chunks."""
    app, hub = app_and_hub()
    sql = wire(
        hub,
        {
            "fetch_hint_json": hint("main.dbx_leaning.results_panel_fit"),
            "SELECT * FROM": [{"chunk_index": 0}],
        },
    )
    with TestClient(app) as client:
        client.get("/api/runs/r1/results")
    assert "ORDER BY chunk_index" in next(s for s, _ in sql.queries if "SELECT * FROM" in s)


# --- the injection gate ----------------------------------------------------


def test_a_valid_name_passes():
    assert validate_table_name("main.dbx_leaning.results_x", catalog="main", schema="dbx_leaning")


@pytest.mark.parametrize(
    "bad",
    [
        "main.dbx_leaning.r; DROP TABLE run_logs",
        "main.dbx_leaning.`quoted`",
        "main.dbx_leaning.r--comment",
        "two.parts",
        "a.b.c.d",
        "main.dbx_leaning.",
        "main.dbx_leaning.1starts_with_digit",
        "main.dbx_leaning.has space",
    ],
)
def test_anything_that_is_not_a_plain_identifier_is_refused(bad):
    """A table name cannot be a bound parameter, so this gate stands in for
    one. The name comes from the job, not a user — but "not user input" is not
    "safe to concatenate"."""
    with pytest.raises(UnsafeTableName):
        validate_table_name(bad, catalog="main", schema="dbx_leaning")


def test_a_table_outside_this_apps_schema_is_refused():
    with pytest.raises(UnsafeTableName, match="outside main.dbx_leaning"):
        validate_table_name("other.place.secrets", catalog="main", schema="dbx_leaning")
    with pytest.raises(UnsafeTableName):
        validate_table_name("main.other_schema.t", catalog="main", schema="dbx_leaning")


def test_a_poisoned_fetch_hint_is_refused_at_the_endpoint(app_and_hub):
    """Defence in depth: the job wrote this value, and a buggy or compromised
    job must not be able to point the app at another schema."""
    app, hub = app_and_hub()
    sql = wire(hub, {"fetch_hint_json": hint("secrets.private.credentials")})

    with TestClient(app) as client:
        resp = client.get("/api/runs/r1/results")

    assert resp.status_code == 422
    assert not any("SELECT * FROM" in s for s, _ in sql.queries), "it must not have read anything"


def test_a_malformed_fetch_hint_is_a_404_not_a_crash(app_and_hub):
    app, hub = app_and_hub()
    wire(hub, {"fetch_hint_json": [{"fetch_hint_json": "not json at all"}]})
    with TestClient(app) as client:
        assert client.get("/api/runs/r1/results").status_code == 404


def test_reading_results_without_a_warehouse_is_a_clean_503(app_and_hub):
    app, hub = app_and_hub()
    assert hub.repo is None
    with TestClient(app) as client:
        assert client.get("/api/runs/r1/results").status_code == 503


def test_the_limit_is_bounded_so_one_request_cannot_pull_everything(app_and_hub):
    app, hub = app_and_hub()
    http = FakeHttp(statement_response([], []))
    hub.sql = SqlClient("https://x", "wh", "t", client=http)
    hub.repo = RunRepository(hub.sql, hub.tables)
    with TestClient(app) as client:
        assert client.get("/api/runs/r1/results", params={"limit": 999_999}).status_code == 422
