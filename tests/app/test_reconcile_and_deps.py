"""Startup reconciliation, and what happens when a service isn't there."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from server.jobs_api import JobsApi
from server.reconcile import reconcile_once
from server.repository import RunRepository
from server.sql import SqlClient
from server.store import StatusTransition
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
    from server.store import WarehouseRunStore

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
    http = FakeHttp({"status": {"state": "TERMINATED", "termination_details": {"code": "SUCCESS"}}})
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

    No `history` method, deliberately: that is what `WarehouseRunStore` looks
    like, and reconciliation has to work against it.
    """

    def __init__(self, rows: list[dict]):
        self.rows = rows
        self.set: list[tuple[str, RunStatus, str | None]] = []
        #: The `ts` each correction was written with, kept apart from `set` so
        #: the assertions above it stay three-tuples.
        self.stamps: list[int | None] = []

    async def non_terminal(self):
        return [SimpleNamespace(as_dict=lambda r=r: r) for r in self.rows]

    async def set_status(self, run_id, status, detail=None, ts=None):
        self.set.append((run_id, status, detail))
        self.stamps.append(ts)


class StoreWithHistory(MemoryStore):
    """A store that also has `run_status_history` — i.e. Lakebase, configured.

    `history` is deliberately not on the `RunStore` Protocol (see
    `store.py::PostgresRunStore.history`), so reconciliation feature-detects it
    on whatever store it was handed. Having one and not having one is therefore
    two classes here, exactly as it is two stores in the app.
    """

    def __init__(self, rows: list[dict], transitions: list, fail: bool = False):
        super().__init__(rows)
        self.transitions = transitions
        self.fail = fail
        self.reads: list[str] = []

    async def history(self, run_id, *, limit=500):
        self.reads.append(run_id)
        if self.fail:
            raise RuntimeError("lakebase unreachable")
        # Sorted here rather than trusting the order the test wrote them in:
        # the real one returns oldest first by the transition's own `ts`,
        # tie-broken by insertion order, and a fake that returned list order
        # would let a test assert behaviour the real store cannot produce.
        rows = [t for t in self.transitions if t.run_id == run_id]
        return sorted(rows, key=lambda t: (t.ts, t.id))[-limit:]


def transition(run_id: str, status: str, ts: int, seq: int) -> StatusTransition:
    return StatusTransition(run_id=run_id, status=status, ts=ts, seq=seq, id=seq)


async def test_the_transition_history_answers_before_the_warehouse_is_woken():
    """Lakebase holds the same transitions `run_events` does, and reading it
    costs no warehouse uptime — which is the cost this platform is built
    around, uptime and not statement count.

    The two sources are given deliberately different answers, so this asserts
    which one won rather than merely that something did.
    """
    repo, sql = repo_for(
        {"ORDER BY seq DESC": [{"status": "FAILED", "detail": "x", "seq": 9, "ts": 1}]}
    )
    store = StoreWithHistory(
        [{"run_id": "r1", "job_run_id": "99", "status": "RUNNING"}],
        [transition("r1", "RUNNING", 1, 0), transition("r1", "SUCCEEDED", 2, 1)],
    )

    report = await reconcile_once(repo, None, store)

    assert report.corrected == [("r1", "SUCCEEDED")]
    assert store.reads == ["r1"]
    assert sql.count("ORDER BY seq DESC") == 0, (
        "the history answered, so nothing should have woken the SQL warehouse"
    )
    assert store.set[0][2] == "reconciled from run_status_history"


async def test_a_transition_recorded_after_a_terminal_one_does_not_hide_it():
    """The history is append-only and keeps what was *reported*, including the
    reports the current-state row's guard refused as stale — so a RUNNING can
    be appended after a SUCCEEDED.

    It does not confuse this read, and the reason is the ordering: the rows
    come back by the transition's OWN `ts`, not by when they were recorded, so
    a redelivered RUNNING sorts back to where it happened.
    """
    store = StoreWithHistory(
        [{"run_id": "r1", "job_run_id": "99", "status": "RUNNING"}],
        [
            transition("r1", "RUNNING", 1, 0),
            transition("r1", "SUCCEEDED", 3, 2),
            transition("r1", "RUNNING", 2, 1),  # redelivered late, appended anyway
        ],
    )

    report = await reconcile_once(None, None, store)

    assert report.corrected == [("r1", "SUCCEEDED")]


async def test_a_retried_task_running_again_is_not_read_as_the_failure_before_it():
    """Why this takes the newest transition rather than the newest TERMINAL
    one anywhere in the log.

    A run can legitimately go terminal and then non-terminal again: Databricks
    retries a failed task and hands the retried attempt the same `run_id`, so
    FAILED is followed by a fresh RUNNING. Resolving that as FAILED would mark
    a live run finished and hand back one of the account's five task slots
    while it is still being used — worse than the alternative, which is simply
    to let the sources behind this one answer.
    """
    store = StoreWithHistory(
        [{"run_id": "r1", "job_run_id": "99", "status": "RUNNING"}],
        [
            transition("r1", "RUNNING", 1, 0),
            transition("r1", "FAILED", 2, 1),
            transition("r1", "RUNNING", 3, 2),  # attempt two
        ],
    )
    jobs = JobsApi("https://x", "t", client=FakeHttp({"status": {"state": "RUNNING"}}))

    report = await reconcile_once(None, jobs, store)

    assert report.still_running == ["r1"] and report.corrected == []
    assert store.set == []


async def test_the_warehouse_is_still_read_when_the_history_has_no_ending():
    """The history is the first source, not the only one. A job whose REST
    report never reached Lakebase still wrote `run_events` on the durable
    path, and that read is what the warehouse wake-up buys."""
    repo, sql = repo_for(
        {"ORDER BY seq DESC": [{"status": "SUCCEEDED", "detail": "done", "seq": 12, "ts": 1}]}
    )
    store = StoreWithHistory(
        [{"run_id": "r1", "job_run_id": "99", "status": "RUNNING"}],
        [transition("r1", "RUNNING", 1, 0)],
    )

    report = await reconcile_once(repo, None, store)

    assert report.corrected == [("r1", "SUCCEEDED")]
    assert store.set[0][2] == "reconciled from run_events"
    assert sql.count("ORDER BY seq DESC") == 1


async def test_a_run_with_no_history_row_at_all_still_reconciles():
    """A run triggered before this table existed, or a job that died before
    its first report, has an empty history and no `run_events` either. The
    Jobs API is the source of last resort and must still be reached."""
    store = StoreWithHistory([{"run_id": "r1", "job_run_id": "99", "status": "RUNNING"}], [])
    http = FakeHttp({"status": {"state": "TERMINATED", "termination_details": {"code": "FAILED"}}})

    report = await reconcile_once(None, JobsApi("https://x", "t", client=http), store)

    assert store.reads == ["r1"], "an empty history is not a reason to skip the read"
    assert report.corrected == [("r1", "FAILED")]


async def test_a_history_read_that_fails_falls_through_instead_of_stranding_the_run():
    """The cheap source failing is not the end of the search.

    Treating it as fatal for the run would turn a Lakebase hiccup into a task
    slot held against the account's five until someone edits the table by
    hand — while the two sources behind it had the answer all along.
    """
    repo, sql = repo_for(
        {"ORDER BY seq DESC": [{"status": "SUCCEEDED", "detail": "done", "seq": 4, "ts": 1}]}
    )
    store = StoreWithHistory(
        [{"run_id": "r1", "job_run_id": "99", "status": "RUNNING"}], [], fail=True
    )

    report = await reconcile_once(repo, None, store)

    assert report.corrected == [("r1", "SUCCEEDED")]
    assert report.errors == [], "a recovered read is not a failure to resolve"


async def test_an_unrecognised_status_in_the_history_is_not_read_as_an_ending():
    """The history keeps what was reported verbatim — `StatusTransition` does
    not map an unknown status onto FAILED the way `RunRecord` does, precisely
    so a data problem stays visible. It must not be resolved as a run's ending
    either: the sources behind it get their turn."""
    repo, sql = repo_for(
        {"ORDER BY seq DESC": [{"status": "CANCELLED", "detail": "by hand", "seq": 2, "ts": 1}]}
    )
    store = StoreWithHistory(
        [{"run_id": "r1", "job_run_id": "99", "status": "RUNNING"}],
        [transition("r1", "RUNNING", 1, 0), transition("r1", "WEDGED", 2, 1)],
    )

    report = await reconcile_once(repo, None, store)

    assert report.corrected == [("r1", "CANCELLED")]
    assert sql.count("ORDER BY seq DESC") == 1


async def test_the_history_is_only_asked_about_the_run_being_reconciled():
    """One read per stale run, scoped to it. A reconciliation that pulled the
    whole table would be reading thousands of rows to answer a question about
    the handful of runs that were live when the app went down."""
    store = StoreWithHistory(
        [
            {"run_id": "r1", "job_run_id": "1", "status": "RUNNING"},
            {"run_id": "r2", "job_run_id": "2", "status": "QUEUED"},
        ],
        [transition("r1", "SUCCEEDED", 2, 1), transition("r2", "FAILED", 2, 1)],
    )

    report = await reconcile_once(None, None, store)

    assert store.reads == ["r1", "r2"]
    assert report.corrected == [("r1", "SUCCEEDED"), ("r2", "FAILED")]


async def test_a_correction_is_written_without_a_message_timestamp():
    """Reconciliation must not stamp its write with a timestamp that could be
    refused.

    `PostgresRunStore.set_status` guards a write that carries a `ts` against
    the row's own — and the timestamps in play here come from another
    machine's clock, so a correction handed one could be rejected as stale.
    The run would then keep one of the account's five task slots, and nothing
    would try again: this pass is startup-only, deliberately.
    """
    store = StoreWithHistory(
        [{"run_id": "r1", "job_run_id": "99", "status": "RUNNING"}],
        [transition("r1", "SUCCEEDED", 2, 1)],
    )

    await reconcile_once(None, None, store)

    assert store.stamps == [None]


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


def test_backfilled_messages_carry_run_id(app_and_hub):
    """A backfilled message must be a `Message`, not merely resemble one.

    `messages_since` selects `seq, ts, type, body` — `run_id` is the bound
    parameter and is not in any of the four UNION branches. It was therefore
    missing from every message the endpoint returned, while
    `BackfillResponse.messages` is typed `Message[]` and every message in the
    envelope carries `run_id`.

    That is not cosmetic. The frontend's single normaliser rejects a message
    with no `run_id` — correctly, since without it a message cannot be stored
    or attributed — so it silently discarded 100% of any backfill routed
    through it. The one existing caller avoided that only by skipping
    normalisation altogether, which left objects typed `Message` whose
    `run_id` was `undefined` at runtime.
    """
    app, hub = app_and_hub()
    http = FakeHttp(
        statement_response(
            ["seq", "ts", "type", "body"],
            [
                [1, 1001, "log", '{"message": "x", "level": "INFO"}'],
                [2, 1002, "progress", '{"elapsed_seconds": 1.5}'],
                [3, 1003, "status", '{"status": "RUNNING"}'],
                [4, 1004, "result", '{"row_count": 7, "chunk_index": 0, "final": true}'],
            ],
        )
    )
    hub.sql = SqlClient("https://x", "wh", "tok", client=http)
    hub.repo = RunRepository(hub.sql, hub.tables)

    with TestClient(app) as client:
        body = client.get("/api/runs/r-backfill/messages").json()

    assert body["messages"], "fixture should return at least one message"
    for message in body["messages"]:
        assert message["run_id"] == "r-backfill", (
            "every backfilled message must carry the run it belongs to"
        )
        # The other three fields every message type shares, so this test
        # fails if the flattening ever drops one of those too.
        assert isinstance(message["seq"], int)
        assert isinstance(message["ts"], int)
        assert message["type"] in {"log", "progress", "status", "result"}
