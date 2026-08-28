"""The job reporting its own status to Lakebase.

`run_status` used to be maintained only by the app, from messages arriving
over the socket — which made a fact about the run depend on the observer
being up. The job knows its own status, so it reports it.

Nothing here is load-bearing, and that is the property most of these assert:
unconfigured, refused or exploding, the run carries on and the durable record
still says what happened.
"""

from __future__ import annotations

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


def record_at(status: str, detail: str | None = None) -> RunRecord:
    record = RunRecord("run-1", model="scenario", job_run_id="jr-7")
    record.observe(make_message("status", run_id="run-1", seq=0, status=status, detail=detail))
    return record


async def test_a_reported_transition_carries_the_records_own_row():
    client = FakeClient(200)
    reporter = LakebaseStatus("https://db/statements", schema="dbx_leaning", client=client)

    assert await reporter.report(record_at("RUNNING", "run started")) is True

    assert reporter.writes == 1 and reporter.failures == 0
    body = client.calls[0]["json"]
    assert "INSERT INTO dbx_leaning.run_status" in body["statement"]
    # Positional, and the order is the contract with $1..$7 in the statement.
    assert body["parameters"][:5] == ["run-1", "jr-7", "scenario", "RUNNING", "run started"]


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
