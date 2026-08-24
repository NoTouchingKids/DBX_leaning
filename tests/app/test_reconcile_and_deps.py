"""Startup reconciliation, and what happens when a service isn't there."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.jobs_api import JobsApi
from app.reconcile import reconcile_once
from app.repository import RunRepository
from app.sql import SqlClient
from shared.envelope import RunStatus
from shared.tables import TableSet

from .conftest import FakeHttp, statement_response


class ScriptedSql:
    """Answers queries by matching on a fragment of the SQL text."""

    def __init__(self, answers: dict[str, list[dict]]):
        self.answers = answers
        self.queries: list[tuple[str, list]] = []
        self.available = True

    async def query(self, sql, params=None):
        self.queries.append((sql, params or []))
        for fragment, rows in self.answers.items():
            if fragment in sql:
                return rows
        return []

    async def close(self): ...

    def count(self, fragment: str) -> int:
        return sum(1 for sql, _ in self.queries if fragment in sql)


def repo_for(answers) -> tuple[RunRepository, ScriptedSql]:
    sql = ScriptedSql(answers)
    return RunRepository(sql, TableSet()), sql


def store_for(repo):
    """Reconciliation reads run state through the store now, not the repo."""
    from app.store import WarehouseRunStore

    return WarehouseRunStore(repo)


async def test_a_run_the_job_finished_while_we_were_down_is_corrected():
    repo, sql = repo_for(
        {
            "NOT IN ('SUCCEEDED'": [{"run_id": "r1", "job_run_id": "99", "status": "RUNNING"}],
            "ORDER BY seq DESC": [{"status": "SUCCEEDED", "detail": "done", "seq": 12, "ts": 1}],
        }
    )
    report = await reconcile_once(repo, None, store_for(repo))

    assert report.checked == 1
    assert report.corrected == [("r1", "SUCCEEDED")]
    assert sql.count("MERGE INTO") == 1, "corrected exactly once"


async def test_the_jobs_api_answers_only_when_the_job_never_recorded_an_ending():
    """run_events is what the job wrote as it went, so it wins. The Jobs API
    is for the crash case, where the job never got to record anything."""
    repo, sql = repo_for(
        {
            "NOT IN ('SUCCEEDED'": [{"run_id": "r1", "job_run_id": "99", "status": "RUNNING"}],
            "ORDER BY seq DESC": [],  # the job never recorded a terminal event
        }
    )
    http = FakeHttp(
        {"status": {"state": "TERMINATED", "termination_details": {"code": "SUCCESS"}}}
    )
    jobs = JobsApi("https://ws.example.com", "tok", client=http)

    report = await reconcile_once(repo, jobs, store_for(repo))
    assert report.corrected == [("r1", "SUCCEEDED")]
    assert http.requests[0]["params"] == {"run_id": "99"}


async def test_a_run_that_really_is_still_going_is_left_alone():
    repo, sql = repo_for(
        {
            "NOT IN ('SUCCEEDED'": [{"run_id": "r1", "job_run_id": "99", "status": "RUNNING"}],
            "ORDER BY seq DESC": [{"status": "RUNNING", "detail": None, "seq": 3, "ts": 1}],
        }
    )
    http = FakeHttp({"status": {"state": "RUNNING"}})
    report = await reconcile_once(repo, JobsApi("https://x", "t", client=http), store_for(repo))

    assert report.still_running == ["r1"] and report.corrected == []
    assert sql.count("MERGE INTO") == 0


async def test_a_cancelled_databricks_run_maps_to_cancelled_not_failed():
    repo, _ = repo_for(
        {
            "NOT IN ('SUCCEEDED'": [{"run_id": "r1", "job_run_id": "99", "status": "RUNNING"}],
            "ORDER BY seq DESC": [],
        }
    )
    http = FakeHttp(
        {"status": {"state": "TERMINATED", "termination_details": {"code": "CANCELED"}}}
    )
    report = await reconcile_once(repo, JobsApi("https://x", "t", client=http), store_for(repo))
    assert report.corrected == [("r1", "CANCELLED")]


class MemoryStore:
    """A run store with no warehouse behind it — i.e. Lakebase.

    Reconciliation used to require a `RunRepository`, which requires the SQL
    warehouse. `run_status` now lives in Postgres, so this is the shape of a
    real deploy, not a test convenience.
    """

    def __init__(self, rows: list[dict]):
        self.rows = rows
        self.set: list[tuple[str, RunStatus, str | None]] = []

    async def non_terminal(self):
        return [SimpleNamespace(as_dict=lambda r=r: r) for r in self.rows]

    async def set_status(self, run_id, status, detail=None):
        self.set.append((run_id, status, detail))


async def test_reconciliation_works_with_no_warehouse_at_all():
    """The configuration this platform actually recommends.

    Gating startup reconciliation on the warehouse repository — which it needs
    only to *sharpen* an answer, never to reach one — meant a Postgres-backed
    deploy with no warehouse silently never reconciled. Every job that died
    before emitting a status then held one of five account-wide task slots
    permanently, so five bad configs finished the platform with no recovery
    short of editing the table by hand.
    """
    store = MemoryStore([{"run_id": "r-dead", "job_run_id": "77", "status": "RUNNING"}])
    http = FakeHttp(
        {"status": {"state": "TERMINATED", "termination_details": {"code": "USER_CANCELED"}}}
    )
    jobs = JobsApi("https://ws.example.com", "tok", client=http)

    report = await reconcile_once(repo=None, jobs=jobs, store=store)

    assert report.checked == 1
    # USER_CANCELED, not CANCELED: this is the code `databricks jobs
    # cancel-run` produces, i.e. the escape hatch the app itself tells users
    # to use when there is no live channel. It was unmapped, so the documented
    # recovery path reconciled a deliberate cancellation as a failure.
    assert report.corrected == [("r-dead", "CANCELLED")]
    assert store.set == [("r-dead", RunStatus.CANCELLED, store.set[0][2])]


async def test_without_a_warehouse_an_unplaceable_run_is_still_left_alone():
    """No warehouse and no answer is not licence to guess.

    The job is autonomous and does not need this app to be up, so a run the
    Jobs API reports as running is running.
    """
    store = MemoryStore([{"run_id": "r-live", "job_run_id": "88", "status": "RUNNING"}])
    jobs = JobsApi("https://x", "t", client=FakeHttp({"status": {"state": "RUNNING"}}))

    report = await reconcile_once(repo=None, jobs=jobs, store=store)

    assert report.still_running == ["r-live"]
    assert store.set == []


async def test_reconciliation_never_blocks_startup_when_the_read_path_is_broken():
    class Broken:
        available = True

        async def query(self, *a, **kw):
            raise RuntimeError("warehouse asleep and unreachable")

        async def close(self): ...

    broken = RunRepository(Broken(), TableSet())
    report = await reconcile_once(broken, None, store_for(broken))
    assert report.errors and report.checked == 0


async def test_reconciliation_reads_once_and_does_not_loop():
    repo, sql = repo_for({"NOT IN ('SUCCEEDED'": []})
    await reconcile_once(repo, None, store_for(repo))
    assert sql.count("NOT IN ('SUCCEEDED'") == 1


def test_a_route_needing_a_missing_service_returns_503_not_an_attribute_error(app_and_hub):
    app, hub = app_and_hub()
    assert hub.repo is None
    with TestClient(app) as client:
        resp = client.get("/api/runs/r1/messages")
    assert resp.status_code == 503
    assert "no SQL warehouse configured" in resp.json()["detail"]


def test_a_route_needing_a_hub_that_never_built_returns_503(app_and_hub):
    app, _ = app_and_hub()
    app.state.hub = None
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 503


def test_health_reports_degradation_rather_than_claiming_to_be_fine(app_and_hub):
    app, hub = app_and_hub()
    with TestClient(app) as client:
        body = client.get("/healthz").json()
    assert body["status"] == "degraded" and "sql" in body["degraded"]


def test_whoami_says_it_is_not_an_authorization_boundary(app_and_hub):
    app, _ = app_and_hub()
    with TestClient(app) as client:
        body = client.get("/api/whoami", headers={"x-forwarded-email": "kp@example.com"}).json()
    assert body["email"] == "kp@example.com" and body["authenticated"] is True
    assert "not an authorization boundary" in body["note"]


def test_backfill_pages_by_seq(app_and_hub):
    app, hub = app_and_hub()
    http = FakeHttp(
        statement_response(
            ["seq", "ts", "type", "body"],
            [[i, 1000 + i, "log", '{"message": "x", "level": "INFO"}'] for i in (1, 2, 3)],
        )
    )
    hub.sql = SqlClient("https://x", "wh", "tok", client=http)
    hub.repo = RunRepository(hub.sql, hub.tables)

    with TestClient(app) as client:
        body = client.get("/api/runs/r1/messages", params={"after_seq": 0, "limit": 3}).json()

    assert body["count"] == 3
    assert body["next_after_seq"] == 3
    assert body["more"] is True, "a full page means the client should ask for another"


@pytest.mark.parametrize("bad", [-5, "abc"])
def test_backfill_rejects_a_nonsense_cursor(app_and_hub, bad):
    app, hub = app_and_hub()
    http = FakeHttp(statement_response(["seq", "ts", "type", "body"], []))
    hub.sql = SqlClient("https://x", "wh", "tok", client=http)
    hub.repo = RunRepository(hub.sql, hub.tables)

    with TestClient(app) as client:
        assert client.get("/api/runs/r1/messages", params={"after_seq": bad}).status_code == 422
    assert http.requests == [], "a rejected cursor must never reach the warehouse"


def test_the_app_serves_the_protocol_schema(app_and_hub):
    """A running client can check the schema it was built against still
    matches the app it is talking to."""
    app, _ = app_and_hub()
    with TestClient(app) as client:
        body = client.get("/api/schema").json()
        assert set(body["$defs"]) == {"envelope", "control"}

        envelope = client.get("/api/schema", params={"kind": "envelope"}).json()
        assert envelope["discriminator"]["propertyName"] == "type"

        assert client.get("/api/schema", params={"kind": "nope"}).status_code == 404


def test_health_advertises_the_protocol_version(app_and_hub):
    from shared.schema import SCHEMA_VERSION

    app, _ = app_and_hub()
    with TestClient(app) as client:
        assert client.get("/healthz").json()["protocol_schema_version"] == SCHEMA_VERSION
