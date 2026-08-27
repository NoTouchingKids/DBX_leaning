"""Surviving a warehouse that is still starting.

Auto-stop is 10 minutes and this is a low-traffic internal tool, so the first
query after any quiet period arrives at a stopped warehouse. That is the normal
case, not an edge one.

The client used to send `on_wait_timeout: CANCEL`, which asks Databricks to
cancel the statement if it has not finished within `wait_timeout`. A cold
2X-Small takes longer than that to come up, so the app cancelled its own
statement and every route failed at once:

    server.sql.StatementError: statement CANCELED: no detail

then recovered ~90 seconds later when the warehouse finished starting, which
made a deterministic failure look intermittent.
"""

from __future__ import annotations

import pytest

from server.sql import P, SqlClient, StatementError


class Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload


def succeeded(statement_id="s1", rows=((1,),), column="n"):
    return {
        "statement_id": statement_id,
        "status": {"state": "SUCCEEDED"},
        "manifest": {"schema": {"columns": [{"name": column}]}},
        "result": {"data_array": [list(r) for r in rows]},
    }


def pending(statement_id="s1", state="PENDING"):
    return {"statement_id": statement_id, "status": {"state": state}}


class FakeHttp:
    """Records what was asked for, and answers from a script."""

    def __init__(self, post_payloads, get_payloads=()):
        self._posts = list(post_payloads)
        self._gets = list(get_payloads)
        self.posted: list[tuple[str, dict]] = []
        self.got: list[str] = []

    async def post(self, url, json=None, headers=None):
        self.posted.append((url, json or {}))
        if url.endswith("/cancel"):
            return Response({}, 200)
        return self._posts.pop(0)

    async def get(self, url, headers=None):
        self.got.append(url)
        return self._gets.pop(0)

    async def aclose(self): ...


def client(http, **kwargs):
    return SqlClient("https://w", "wh1", "tok", client=http, **kwargs)


class TestTheRequestItself:
    async def test_the_statement_is_not_cancelled_on_wait_timeout(self):
        """CANCEL is what broke it. A statement left to CONTINUE stays queued
        behind the starting warehouse, and the client waits for it."""
        http = FakeHttp([Response(succeeded())])
        await client(http).query("SELECT 1")

        _, body = http.posted[0]
        assert body["on_wait_timeout"] == "CONTINUE"

    async def test_a_warm_warehouse_is_still_one_round_trip(self):
        """The reason for using the API's own `wait_timeout` at all. Polling
        must not become the normal path."""
        http = FakeHttp([Response(succeeded(rows=((7,),)))])
        assert await client(http).query("SELECT 1") == [{"n": 7}]
        assert http.got == [], "a terminal first answer needs no polling"


class TestWaitingForAColdWarehouse:
    async def test_a_pending_statement_is_polled_until_it_finishes(self, monkeypatch):
        monkeypatch.setattr("server.sql.asyncio.sleep", _no_sleep)
        http = FakeHttp(
            [Response(pending())],
            [Response(pending(state="RUNNING")), Response(succeeded(rows=((3,),)))],
        )
        assert await client(http).query("SELECT 1") == [{"n": 3}]
        assert len(http.got) == 2

    async def test_polling_backs_off(self, monkeypatch):
        """A three-minute wait should be a handful of requests, not hundreds."""
        slept: list[float] = []

        async def record(seconds):
            slept.append(seconds)

        monkeypatch.setattr("server.sql.asyncio.sleep", record)
        http = FakeHttp([Response(pending())], [Response(pending())] * 5 + [Response(succeeded())])
        await client(http).query("SELECT 1")

        assert slept == sorted(slept), "delays must not shrink"
        assert slept[-1] > slept[0]
        assert max(slept) <= 5.0, "and must stay bounded"

    async def test_giving_up_cancels_the_statement_it_left_behind(self, monkeypatch):
        """Otherwise a statement keeps running for a caller that has gone."""
        monkeypatch.setattr("server.sql.asyncio.sleep", _no_sleep)
        monkeypatch.setattr("server.sql.time.monotonic", _clock(step=10.0))

        http = FakeHttp([Response(pending("slow"))], [Response(pending("slow"))] * 50)
        with pytest.raises(StatementError, match="warehouse may still be starting"):
            await client(http, statement_deadline_s=20.0).query("SELECT 1")

        assert any(url.endswith("/statements/slow/cancel") for url, _ in http.posted)

    async def test_the_deadline_is_never_shorter_than_the_first_wait(self):
        """A deadline under `wait_timeout` would expire before the first answer
        came back, making polling unreachable and every cold start a failure."""
        c = client(FakeHttp([]), wait_timeout_s=30, statement_deadline_s=5.0)
        assert c.statement_deadline_s == 30


class TestWhatTheErrorSays:
    async def test_a_cancelled_statement_no_longer_reports_no_detail(self):
        """The API sends no message for a statement cancelled by the wait flag,
        so "no detail" was accurate and useless. The id is always there."""
        http = FakeHttp([Response({"statement_id": "abc", "status": {"state": "CANCELED"}})])
        with pytest.raises(StatementError) as caught:
            await client(http).query("SELECT 1")

        assert "no detail" not in str(caught.value)
        assert "statement_id=abc" in str(caught.value)

    async def test_a_real_error_message_is_kept(self):
        http = FakeHttp(
            [
                Response(
                    {
                        "statement_id": "abc",
                        "status": {"state": "FAILED", "error": {"message": "TABLE_NOT_FOUND"}},
                    }
                )
            ]
        )
        with pytest.raises(StatementError, match="TABLE_NOT_FOUND"):
            await client(http).query("SELECT 1")

    async def test_a_non_terminal_answer_with_nothing_to_poll_does_not_spin(self):
        http = FakeHttp([Response({"status": {"state": "PENDING"}})])
        with pytest.raises(StatementError, match="no statement_id to poll"):
            await client(http).query("SELECT 1")

    async def test_a_failed_poll_is_reported_rather_than_retried_forever(self, monkeypatch):
        monkeypatch.setattr("server.sql.asyncio.sleep", _no_sleep)
        http = FakeHttp([Response(pending())], [Response({"x": 1}, status_code=500)])
        with pytest.raises(StatementError, match="polling s1 failed: HTTP 500"):
            await client(http).query("SELECT 1")


async def test_bound_parameters_still_travel_with_their_types():
    """Guarding the rule this module exists for while its transport changed."""
    http = FakeHttp([Response(succeeded())])
    await client(http).query("SELECT :n", [P.int("n", 12)])

    _, body = http.posted[0]
    assert body["parameters"] == [{"name": "n", "value": "12", "type": "INT"}]


async def _no_sleep(_seconds):
    return None


def _clock(step: float):
    """A monotonic clock that advances by `step` on every read, so a deadline
    can be reached without waiting for one."""
    now = [0.0]

    def tick() -> float:
        now[0] += step
        return now[0]

    return tick
