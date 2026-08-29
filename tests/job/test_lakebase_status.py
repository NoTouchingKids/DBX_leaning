"""The job reporting its own status to Lakebase.

`run_status` used to be maintained only by the app, from messages arriving
over the socket — which made a fact about the run depend on the observer
being up. The job knows its own status, so it reports it.

One report writes two rows — `run_status`, the current state, and
`run_status_history`, the transition log — in one statement. Most of what is
asserted here is the statement and the parameters the client is actually
handed, because an edit that drops the history append would otherwise leave
every other test in this file passing.

Nothing here is load-bearing, and that is the other property most of these
assert: unconfigured, refused or exploding, the run carries on and the durable
record still says what happened.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

from job.auth import AppCredential
from job.lakebase import LakebaseStatus
from job.record import RunRecord
from job.shared.envelope import make_message


class FakeClient:
    def __init__(self, status_code: int = 200, raises: Exception | None = None) -> None:
        self.status_code = status_code
        self.raises = raises
        self.calls: list[dict] = []
        self.closed = False

    async def post(self, url, json=None, headers=None):
        self.calls.append({"url": url, "json": json, "headers": headers or {}})
        if self.raises is not None:
            raise self.raises
        return SimpleNamespace(status_code=self.status_code, text="server said no")

    async def aclose(self):
        self.closed = True


def record_at(
    status: str, detail: str | None = None, *, seq: int = 0, ts: int | None = None
) -> RunRecord:
    record = RunRecord("run-1", model="scenario", job_run_id="jr-7")
    record.observe(
        make_message("status", run_id="run-1", seq=seq, ts=ts, status=status, detail=detail)
    )
    return record


def sent(client: FakeClient) -> tuple[str, list]:
    """The one statement and its parameters, as the REST API would get them."""
    assert len(client.calls) == 1, "one transition is one round trip"
    body = client.calls[0]["json"]
    return body["statement"], body["parameters"]


async def test_a_reported_transition_carries_the_records_own_row():
    client = FakeClient(200)
    reporter = LakebaseStatus("https://db/statements", schema="dbx_leaning", client=client)

    assert await reporter.report(record_at("RUNNING", "run started")) is True

    assert reporter.writes == 1 and reporter.failures == 0
    statement, parameters = sent(client)
    # `run_status` is a prefix of `run_status_history`, so the current-state
    # insert has to be matched with the newline that follows its table name —
    # without it this passes on the history insert alone, and the assertion
    # stops being able to fail the way it exists to fail.
    assert "INSERT INTO dbx_leaning.run_status\n" in statement
    # Positional, and the order is the contract with $1..$9 in the statement.
    assert parameters[:5] == ["run-1", "jr-7", "scenario", "RUNNING", "run started"]


async def test_a_rejected_write_is_counted_and_not_raised():
    client = FakeClient(500)
    reporter = LakebaseStatus("https://db/statements", client=client)

    assert await reporter.report(record_at("SUCCEEDED")) is False

    assert reporter.writes == 0 and reporter.failures == 1
    assert "HTTP 500" in (reporter.last_error or "")


async def test_a_client_that_explodes_is_counted_and_not_raised():
    """Unreachable Lakebase is a live-path problem. `run_events` and the
    end-of-run Delta write still carry the outcome."""
    client = FakeClient(raises=ConnectionError("no route to host"))
    reporter = LakebaseStatus("https://db/statements", client=client)

    assert await reporter.report(record_at("FAILED")) is False

    assert reporter.failures == 1
    assert "ConnectionError" in (reporter.last_error or "")


async def test_an_unconfigured_reporter_says_nothing_to_nobody():
    """No `DBX_LAKEBASE_REST_URL` is a normal deploy, not a broken one."""
    client = FakeClient()
    reporter = LakebaseStatus("", client=client)

    assert reporter.available is False
    assert await reporter.report(record_at("RUNNING")) is False
    assert client.calls == [] and reporter.failures == 0


async def test_the_databricks_token_travels_on_the_request():
    """The Database REST API is behind the same OAuth the Apps ingress wants —
    see job/auth.py for why the app's own shared secret is a different header."""
    client = FakeClient(200)
    reporter = LakebaseStatus(
        "https://db/statements",
        credential=AppCredential(env={"DBX_APP_OAUTH_TOKEN": "oauth-token"}),
        client=client,
    )

    await reporter.report(record_at("RUNNING"))

    assert client.calls[0]["headers"]["Authorization"] == "Bearer oauth-token"


async def test_an_injected_client_is_not_closed_out_from_under_its_owner():
    client = FakeClient()
    reporter = LakebaseStatus("https://db/statements", client=client)

    await reporter.close()

    assert client.closed is False


# --- the history row, appended alongside the current one -------------------


async def test_one_report_writes_the_current_row_and_its_history_in_one_statement():
    """One round trip, one transaction. Two requests would be two failure
    modes, and the one that fails second leaves history holding a transition
    the current-state row never got — a worse record than either table alone.
    """
    client = FakeClient(200)
    reporter = LakebaseStatus("https://db/statements", schema="dbx_leaning", client=client)

    await reporter.report(record_at("RUNNING", "run started"))

    statement, _ = sent(client)
    assert statement.startswith("WITH upsert_current AS (")
    assert "INSERT INTO dbx_leaning.run_status\n" in statement
    assert "INSERT INTO dbx_leaning.run_status_history" in statement


async def test_the_history_row_carries_the_status_messages_own_seq_and_ts():
    """The history table dedupes on (run_id, seq). Bind anything but the
    message's own seq — a counter of this reporter's writes, say — and a
    report redelivered after a retry appends a second row for one transition.
    """
    client = FakeClient(200)
    reporter = LakebaseStatus("https://db/statements", client=client)

    await reporter.report(record_at("SUCCEEDED", "done", seq=12, ts=1_700))

    statement, parameters = sent(client)
    assert parameters[7] == 12 and parameters[8] == 1_700
    # $8/$9 are the history row's alone; run_id, status and detail it shares
    # with the current row rather than repeating, so the two rows cannot end
    # up describing different transitions.
    assert "VALUES ($1, $8, $4, $5, $9, 'job')" in statement


async def test_the_out_of_order_guard_still_protects_the_current_row():
    """The app writes this row too and Databricks can deliver a retry out of
    order; without the guard a late RUNNING overwrites a SUCCEEDED that has
    already landed."""
    client = FakeClient(200)
    reporter = LakebaseStatus("https://db/statements", client=client)

    await reporter.report(record_at("RUNNING"))

    statement, _ = sent(client)
    assert "WHERE dbx_leaning.run_status.updated_ts <= EXCLUDED.updated_ts" in statement


async def test_the_history_append_sits_outside_the_guarded_upsert():
    """It looks like a bug and is the point: when the guard makes the upsert a
    no-op, the history row still appends. Current state is what is true, so a
    stale transition must not move it backwards; history is what was
    *reported*, and that the stale one arrived is the fact you want later.

    Structural, because it is the placement that does it: the guard has to
    close with the CTE, before the INSERT that appends.
    """
    client = FakeClient(200)
    reporter = LakebaseStatus("https://db/statements", client=client)

    await reporter.report(record_at("RUNNING"))

    statement, _ = sent(client)
    guard = statement.index("WHERE dbx_leaning.run_status.updated_ts <= EXCLUDED.updated_ts")
    append = statement.index("INSERT INTO dbx_leaning.run_status_history")
    assert guard < append
    assert ")\nINSERT INTO dbx_leaning.run_status_history" in statement
    # And a retried report of the same message is one history row, not two.
    assert "ON CONFLICT DO NOTHING" in statement


async def test_a_report_before_any_status_message_binds_a_null_seq():
    """A job that died before emitting anything still reports, and NULL seq is
    what keeps that row outside the history table's partial unique index —
    there is no message identity to dedupe it by."""
    client = FakeClient(200)
    reporter = LakebaseStatus("https://db/statements", client=client)

    assert await reporter.report(RunRecord("run-1", model="scenario")) is True

    _, parameters = sent(client)
    assert parameters[3] == "FAILED", "nothing arrived is not a success"
    assert parameters[7] is None
    assert parameters[8] == parameters[6], "no message clock, so the report's own"


async def test_every_placeholder_in_the_statement_is_bound_and_no_parameter_is_spare():
    """Binding is positional: a parameter inserted anywhere but the end
    rebinds every one after it, silently, and Postgres would read a detail as
    a started_ts. `tests/job/test_runner.py` reads the status at index 3."""
    client = FakeClient(200)
    reporter = LakebaseStatus("https://db/statements", client=client)

    await reporter.report(record_at("RUNNING"))

    statement, parameters = sent(client)
    assert len(parameters) == 9
    assert {int(n) for n in re.findall(r"\$(\d+)", statement)} == set(range(1, 10))


async def test_both_tables_are_qualified_with_the_configured_schema():
    """Every statement qualifies its table rather than trusting a search_path:
    this posts one request per transition, and a session that had reverted to
    `public` would find a different, empty table instead of failing."""
    client = FakeClient(200)
    reporter = LakebaseStatus("https://db/statements", schema="other_schema", client=client)

    await reporter.report(record_at("RUNNING"))

    statement, _ = sent(client)
    assert "other_schema.run_status\n" in statement
    assert "other_schema.run_status_history" in statement
    assert "dbx_leaning" not in statement and "{schema}" not in statement
