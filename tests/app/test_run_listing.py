"""Filtering GET /api/runs.

Server-side, and by bound parameter. Client-side filtering over the fetched
top-N window is only correct while that window happens to contain everything
relevant, and it fails silently when it does not.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.repository import RunRepository
from app.sql import SqlClient
from app.store import WarehouseRunStore
from shared.tables import TableSet

from .conftest import FakeHttp, statement_response

ROW_COLUMNS = [
    "run_id",
    "job_run_id",
    "model",
    "status",
    "detail",
    "started_ts",
    "updated_ts",
    "requested_by",
]


def repo(http) -> RunRepository:
    return RunRepository(
        SqlClient("https://ws.example.com", "wh-1", "tok", client=http), TableSet()
    )


def sent(http) -> tuple[str, dict]:
    body = http.requests[0]["json"]
    return body["statement"], {p["name"]: p for p in body["parameters"]}


# --- the repository query ---------------------------------------------------


async def test_the_model_filter_is_a_bound_parameter_not_interpolation():
    http = FakeHttp(statement_response(ROW_COLUMNS, []))
    await repo(http).list_runs(limit=10, model="mcmc")

    sql, params = sent(http)
    assert "WHERE model = :model" in sql
    assert params["model"] == {"name": "model", "value": "mcmc", "type": "STRING"}
    assert "mcmc" not in sql


async def test_a_hostile_model_name_never_reaches_the_sql_text():
    http = FakeHttp(statement_response(ROW_COLUMNS, []))
    await repo(http).list_runs(model="'; DROP TABLE run_status; --")

    sql, params = sent(http)
    assert "DROP TABLE" not in sql
    assert params["model"].get("value") == "'; DROP TABLE run_status; --"


async def test_status_and_model_combine_with_and():
    http = FakeHttp(statement_response(ROW_COLUMNS, []))
    await repo(http).list_runs(limit=5, status="RUNNING", model="mcmc")

    sql, params = sent(http)
    assert "WHERE status = :status AND model = :model" in sql
    assert set(params) == {"status", "model", "row_limit"}
    assert params["row_limit"]["type"] == "INT"


async def test_no_filters_means_no_where_clause_and_no_orphan_parameters():
    """A declared parameter the statement never references is rejected at
    execution time, which is why the clause and its parameters are built as
    one thing rather than by branching."""
    http = FakeHttp(statement_response(ROW_COLUMNS, []))
    await repo(http).list_runs(limit=7)

    sql, params = sent(http)
    assert "WHERE" not in sql
    assert set(params) == {"row_limit"}


@pytest.mark.parametrize(
    "filters,expected",
    [
        ({}, set()),
        ({"status": "RUNNING"}, {"status"}),
        ({"model": "mcmc"}, {"model"}),
        ({"status": "RUNNING", "model": "mcmc"}, {"status", "model"}),
    ],
)
async def test_every_parameter_declared_is_referenced_and_vice_versa(filters, expected):
    http = FakeHttp(statement_response(ROW_COLUMNS, []))
    await repo(http).list_runs(**filters)

    sql, params = sent(http)
    assert set(params) - {"row_limit"} == expected
    for name in params:
        assert f":{name}" in sql


# --- the route --------------------------------------------------------------


@pytest.fixture
def listing(app_and_hub, config):
    def _make(rows):
        app, hub = app_and_hub(config())
        http = FakeHttp(statement_response(ROW_COLUMNS, rows))
        hub.repo = repo(http)
        hub.store = WarehouseRunStore(hub.repo)
        return app, http

    return _make


def row(run_id, model, status="SUCCEEDED"):
    return [run_id, "1", model, status, None, 1, 2, "kp"]


def test_the_model_query_param_reaches_the_statement(listing):
    app, http = listing([row("r1", "mcmc")])
    with TestClient(app) as client:
        body = client.get("/api/runs", params={"model": "mcmc"}).json()

    assert body["filters"] == {"status": None, "model": "mcmc"}
    assert [r["model"] for r in body["runs"]] == ["mcmc"]
    sql, params = sent(http)
    assert params["model"]["value"] == "mcmc"


def test_status_and_model_can_be_combined_from_the_route(listing):
    app, http = listing([row("r1", "mcmc", "RUNNING")])
    with TestClient(app) as client:
        body = client.get("/api/runs", params={"model": "mcmc", "status": "RUNNING"}).json()

    assert body["filters"] == {"status": "RUNNING", "model": "mcmc"}
    sql, params = sent(http)
    assert params["model"]["value"] == "mcmc"
    assert params["status"]["value"] == "RUNNING"
    assert "WHERE status = :status AND model = :model" in sql


def test_no_model_param_leaves_the_listing_unfiltered(listing):
    app, http = listing([row("r1", "mcmc"), row("r2", "scenario")])
    with TestClient(app) as client:
        body = client.get("/api/runs").json()

    assert body["count"] == 2
    sql, params = sent(http)
    assert "WHERE" not in sql
    assert set(params) == {"row_limit"}
