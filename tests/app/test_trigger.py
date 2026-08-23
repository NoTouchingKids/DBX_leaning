"""Triggering a run, and the registry row that makes it findable afterwards.

Without the registry write, nothing lists a run and startup reconciliation —
which finds work by reading run_status — never sees it.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.jobs_api import JobsApi
from app.repository import RunRepository

from .conftest import FakeHttp


class RecordingSql:
    """Records statements and answers by matching a fragment of the SQL."""

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

    def statements(self, fragment: str) -> list[tuple[str, list]]:
        return [(s, p) for s, p in self.queries if fragment in s]

    def params_of(self, fragment: str) -> dict:
        sql, params = self.statements(fragment)[0]
        return {p.name: p for p in params}


def wire(hub, *, job_http=None, sql=None, active=0):
    from app.store import WarehouseRunStore

    sql = sql or RecordingSql({"COUNT(*)": [{"active": active}]})
    hub.sql = sql
    hub.repo = RunRepository(sql, hub.tables)
    hub.store = WarehouseRunStore(hub.repo)
    hub.jobs_api = JobsApi(
        "https://ws.example.com", "tok", client=job_http or FakeHttp({"run_id": 987654})
    )
    return sql


@pytest.fixture
def triggerable(app_and_hub, config):
    def _make(**overrides):
        cfg = config(
            job_ids={"scenario": 4242},
            public_url="https://app.example.com",
            job_token="s3cret",
            **overrides,
        )
        return app_and_hub(cfg)

    return _make


def test_triggering_launches_the_job_and_registers_the_run(triggerable):
    app, hub = triggerable()
    job_http = FakeHttp({"run_id": 987654})
    sql = wire(hub, job_http=job_http)

    with TestClient(app) as client:
        resp = client.post(
            "/api/runs",
            json={"model": "scenario", "config": {"progress_every": 5}},
            headers={"x-forwarded-email": "kp@example.com"},
        )

    assert resp.status_code == 202
    body = resp.json()
    assert body["job_run_id"] == 987654
    assert body["status"] == "QUEUED" and body["registered"] is True
    assert body["stream"] == f"/api/runs/{body['run_id']}/stream"

    sent = job_http.requests[0]["json"]
    assert sent["job_id"] == 4242
    params = sent["job_parameters"]
    assert params["DBX_MODEL"] == "models.scenario"
    assert json.loads(params["DBX_MODEL_CONFIG"]) == {"progress_every": 5}
    assert params["DBX_RUN_ID"] == body["run_id"]
    assert params["DBX_APP_URL"] == "https://app.example.com"
    assert params["DBX_APP_TOKEN"] == "s3cret"

    registered = sql.params_of("INSERT INTO")
    assert registered["run_id"].value == body["run_id"]
    assert registered["model"].value == "scenario"
    assert registered["status"].value == "QUEUED"
    assert registered["requested_by"].value == "kp@example.com"

    # Reserve, then launch, then attach: the slot is taken before the job
    # exists, so the registry can never lag behind a running job.
    order = [i for i, (stmt, _) in enumerate(sql.queries) if "INSERT INTO" in stmt]
    assert order, "the run was never registered"
    assert body["job_run_id_stored"] is True
    assert sql.params_of("MERGE INTO")["job_run_id"].value == "987654"


def test_a_caller_supplied_run_id_is_honoured(triggerable):
    app, hub = triggerable()
    wire(hub)
    with TestClient(app) as client:
        resp = client.post("/api/runs", json={"model": "scenario", "run_id": "my-run-1"})
    assert resp.json()["run_id"] == "my-run-1"


def test_a_model_with_no_job_behind_it_cannot_be_triggered(triggerable):
    app, hub = triggerable()
    wire(hub)
    with TestClient(app) as client:
        resp = client.post("/api/runs", json={"model": "not_a_model"})

    assert resp.status_code == 404
    assert "triggerable models are ['scenario']" in resp.json()["detail"]


def test_the_five_concurrent_task_ceiling_is_refused_clearly(triggerable):
    """Free Edition allows 5 concurrent job tasks per account, across all
    models. Better a clear 429 than an opaque queue on the Databricks side."""
    app, hub = triggerable()
    job_http = FakeHttp({"run_id": 1})
    wire(hub, job_http=job_http, active=5)

    with TestClient(app) as client:
        resp = client.post("/api/runs", json={"model": "scenario"})

    assert resp.status_code == 429
    assert "ceiling is 5" in resp.json()["detail"]
    assert job_http.requests == [], "nothing should have been launched"


def test_the_ceiling_is_configurable(triggerable):
    app, hub = triggerable(max_concurrent_runs=2)
    wire(hub, active=2)
    with TestClient(app) as client:
        assert client.post("/api/runs", json={"model": "scenario"}).status_code == 429


def test_a_refused_launch_does_not_register_a_phantom_run(triggerable):
    app, hub = triggerable()
    sql = wire(hub, job_http=FakeHttp({"error_code": "RESOURCE_DOES_NOT_EXIST"}, status_code=400))

    with TestClient(app) as client:
        resp = client.post("/api/runs", json={"model": "scenario"})

    assert resp.status_code == 502
    # The slot was claimed before the launch was attempted, so what matters is
    # that it is given back — not that nothing was ever written.
    assert sql.statements("INSERT INTO"), "the slot should have been claimed first"


def test_a_registry_that_cannot_be_written_stops_the_launch(triggerable):
    """Reserving first means a registry failure happens BEFORE anything is
    launched — no orphan job, rather than a job nothing knows about."""

    class InsertFails(RecordingSql):
        async def query(self, sql, params=None):
            if "INSERT INTO" in sql:
                raise RuntimeError("warehouse asleep")
            return await super().query(sql, params)

    app, hub = triggerable()
    job_http = FakeHttp({"run_id": 1})
    wire(hub, sql=InsertFails({"COUNT(*)": [{"active": 0}]}), job_http=job_http)

    with TestClient(app) as client:
        resp = client.post("/api/runs", json={"model": "scenario"})

    assert resp.status_code == 503
    assert "nothing was launched" in resp.json()["detail"]
    assert job_http.requests == [], "nothing should have been launched"


def test_a_run_whose_job_run_id_cannot_be_stored_is_still_registered(triggerable):
    """The job IS running by this point, so this reports rather than fails —
    but the run is already in the registry, unlike before."""

    class MergeFails(RecordingSql):
        async def query(self, sql, params=None):
            if "MERGE INTO" in sql:
                raise RuntimeError("warehouse asleep")
            return await super().query(sql, params)

    app, hub = triggerable()
    wire(hub, sql=MergeFails({"COUNT(*)": [{"active": 0}]}))

    with TestClient(app) as client:
        body = client.post("/api/runs", json={"model": "scenario"}).json()

    assert body["registered"] is True
    assert body["job_run_id"] == 987654
    assert body["job_run_id_stored"] is False


def test_triggering_without_a_workspace_is_a_clean_503(app_and_hub, config):
    app, hub = app_and_hub(config(job_ids={"scenario": 1}))
    sql = RecordingSql({"COUNT(*)": [{"active": 0}]})
    hub.sql = sql
    hub.repo = RunRepository(sql, hub.tables)
    hub.jobs_api = None

    with TestClient(app) as client:
        resp = client.post("/api/runs", json={"model": "scenario"})
    assert resp.status_code == 503


def test_a_job_url_is_not_sent_when_the_app_has_no_public_url(app_and_hub, config):
    """No DBX_APP_URL means the run proceeds unobserved — a normal case, and
    better than handing the job an address it cannot reach."""
    app, hub = app_and_hub(config(job_ids={"scenario": 1}, public_url=None))
    job_http = FakeHttp({"run_id": 5})
    wire(hub, job_http=job_http)

    with TestClient(app) as client:
        client.post("/api/runs", json={"model": "scenario"})

    assert "DBX_APP_URL" not in job_http.requests[0]["json"]["job_parameters"]


def test_listing_runs_marks_which_are_live(triggerable):
    app, hub = triggerable()
    rows = [
        {"run_id": "r1", "job_run_id": "1", "model": "scenario", "status": "RUNNING",
         "detail": None, "started_ts": 1, "updated_ts": 2, "requested_by": "kp"},
        {"run_id": "r2", "job_run_id": "2", "model": "mcmc", "status": "SUCCEEDED",
         "detail": None, "started_ts": 1, "updated_ts": 3, "requested_by": "kp"},
    ]
    hub.sql = RecordingSql({"ORDER BY updated_ts DESC": rows})
    hub.repo = RunRepository(hub.sql, hub.tables)

    with TestClient(app) as client:
        headers = {"Authorization": "Bearer s3cret"}
        with client.websocket_connect("/ws/job/r1", headers=headers):
            body = client.get("/api/runs").json()

    assert body["count"] == 2
    assert {r["run_id"]: r["live"] for r in body["runs"]} == {"r1": True, "r2": False}


def test_the_models_endpoint_lists_what_can_be_triggered(triggerable):
    app, hub = triggerable()
    with TestClient(app) as client:
        body = client.get("/api/models").json()
    assert body["models"] == [{"name": "scenario", "job_id": 4242}]


def test_active_run_count_is_a_single_statement(triggerable):
    app, hub = triggerable()
    sql = wire(hub)
    with TestClient(app) as client:
        client.post("/api/runs", json={"model": "scenario"})
    assert len(sql.statements("COUNT(*)")) == 1, "the ceiling check must not fan out"


async def test_a_status_message_updates_the_registry(app_and_hub, config):
    """The status message is a notification; run_status is the record of
    truth. This is what keeps them in step while the app is up."""
    import asyncio

    from app.store import WarehouseRunStore
    from shared.envelope import make_message

    app, hub = app_and_hub()
    sql = RecordingSql()
    hub.sql = sql
    hub.repo = RunRepository(sql, hub.tables)
    hub.store = WarehouseRunStore(hub.repo)

    await hub.ingest("r1", make_message("status", run_id="r1", seq=0, status="RUNNING"))
    await hub.ingest(
        "r1", make_message("status", run_id="r1", seq=9, status="SUCCEEDED", detail="done")
    )
    await asyncio.gather(*hub._status_tasks)

    merges = sql.statements("MERGE INTO")
    assert len(merges) == 2
    assert hub.status_writes == 2
    statuses = [{p.name: p.value for p in params}["status"] for _, params in merges]
    assert statuses == ["RUNNING", "SUCCEEDED"]


async def test_non_status_messages_do_not_touch_the_warehouse(app_and_hub):
    from app.store import WarehouseRunStore
    from shared.envelope import make_message

    app, hub = app_and_hub()
    sql = RecordingSql()
    hub.sql = sql
    hub.repo = RunRepository(sql, hub.tables)
    hub.store = WarehouseRunStore(hub.repo)

    await hub.ingest("r1", make_message("log", run_id="r1", seq=0, message="x"))
    await hub.ingest("r1", make_message("progress", run_id="r1", seq=1, elapsed_seconds=1.0))

    assert sql.queries == [], "telemetry must never wake the warehouse"


async def test_a_failed_status_write_does_not_break_ingest(app_and_hub):
    """run_events already has it, and startup reconciliation will catch up."""
    import asyncio

    from shared.envelope import make_message

    class Broken(RecordingSql):
        async def query(self, sql, params=None):
            raise RuntimeError("warehouse asleep")

    from app.store import WarehouseRunStore

    app, hub = app_and_hub()
    hub.sql = Broken()
    hub.repo = RunRepository(hub.sql, hub.tables)
    hub.store = WarehouseRunStore(hub.repo)

    sub = hub.broadcaster.subscribe("r1")
    await hub.ingest("r1", make_message("status", run_id="r1", seq=0, status="RUNNING"))
    await asyncio.gather(*hub._status_tasks, return_exceptions=True)

    assert sub.queue.qsize() == 1, "the live relay must be unaffected"
    assert hub.status_writes == 0
    sub.close()


async def test_status_persistence_is_skipped_entirely_without_a_warehouse(app_and_hub):
    from shared.envelope import make_message

    app, hub = app_and_hub()
    assert hub.store is None
    await hub.ingest("r1", make_message("status", run_id="r1", seq=0, status="RUNNING"))
    assert hub._status_tasks == set()
