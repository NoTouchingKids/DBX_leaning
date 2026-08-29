"""Where a backfill is answered from: the job's memory, or the warehouse.

On Databricks, warehouse cost is *uptime*, not statement count. A tab
backgrounded for thirty seconds reconnects with a gap of a few dozen messages,
and serving that from Unity Catalog wakes a warehouse for a question the job
holding the run can answer out of its own replay ring in one WebSocket frame.
Every test in here is a version of "did that read wake the warehouse".

The boundary between the two sources is a fact on the wire — the job states
the oldest seq it can still replay — and never a threshold either side keeps.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from fastapi.testclient import TestClient

from server.repository import RunRepository
from server.routes import runs as runs_routes
from server.routes.ingest import _handle_control
from server.services import JobConnections
from server.sql import SqlClient
from shared.codec import to_jsonable
from shared.envelope import make_message
from shared.protocol import (
    ControlFrame,
    ControlKind,
    backfill_result,
    hello,
    pack_frame,
    unpack_frame,
)
from shared.tables import TableSet

from .conftest import FakeHttp, statement_response


class WarehouseTripwire:
    """A SQL client that fails the test rather than answering it.

    Injected wherever the claim is "this read never reached the warehouse":
    asserting on a call count would pass just as well if the query were made
    and its result thrown away, and the thing being protected is the wake-up,
    not the row.
    """

    available = True

    async def query(self, *args, **kwargs):
        raise AssertionError("a backfill the job could answer went to the warehouse")

    async def close(self): ...


class FakeJob:
    """A job's end of the socket, answering the way ``job/bus.py`` does.

    It replies *inside* the send, which is what makes a synchronous test of
    the route possible at all: ``TestClient.get`` blocks the calling thread
    until the response is finished, so a reply the test itself had to send
    while the request was in flight would deadlock rather than fail.

    The reply goes back through the app's real inbound handler, so what is
    under test includes ``ingest._handle_control`` rather than only the bit of
    ``JobConnections`` it calls.
    """

    def __init__(
        self,
        connections: JobConnections,
        run_id: str,
        *,
        messages: list[dict] | None = None,
        complete: bool = True,
        replay_from_seq: int = 0,
        flushed_through_seq: int = 0,
        answer: bool = True,
    ) -> None:
        self.run_id = run_id
        self.messages = messages or []
        self.complete = complete
        self.replay_from_seq = replay_from_seq
        self.flushed_through_seq = flushed_through_seq
        self.answer = answer
        self.frames: list[ControlFrame] = []
        # `_handle_control` only ever reaches `hub.job_sockets`; the rest of a
        # ServiceHub is not part of this conversation.
        self._hub = SimpleNamespace(job_sockets=connections)

    @property
    def asked(self) -> list[ControlFrame]:
        return [f for f in self.frames if f.kind is ControlKind.BACKFILL]

    async def send_bytes(self, raw: bytes) -> None:
        frame = unpack_frame(raw)
        assert isinstance(frame, ControlFrame)
        self.frames.append(frame)
        if frame.kind is not ControlKind.BACKFILL or not self.answer:
            return
        after = int(frame.payload["after_seq"])
        limit = frame.payload["limit"]
        served = [m for m in self.messages if m["seq"] > after]
        if limit is not None:
            served = served[:limit]
        await _handle_control(
            self._hub,
            self,
            self.run_id,
            backfill_result(
                self.run_id,
                after_seq=after,
                messages=served,
                complete=self.complete,
                replay_from_seq=self.replay_from_seq,
                flushed_through_seq=self.flushed_through_seq,
            ),
        )


class SilentJob:
    """A job that takes the frame and never answers: a wedged event loop, or a
    process that died between the send and the reply."""

    def __init__(self) -> None:
        self.frames: list[ControlFrame] = []

    @property
    def asked(self) -> list[ControlFrame]:
        return [f for f in self.frames if f.kind is ControlKind.BACKFILL]

    async def send_bytes(self, raw: bytes) -> None:
        frame = unpack_frame(raw)
        assert isinstance(frame, ControlFrame)
        self.frames.append(frame)


def log_messages(run_id: str, seqs: list[int]) -> list[dict]:
    """Envelope-shaped dicts, exactly as the job puts them on the wire."""
    return [
        to_jsonable(make_message("log", run_id=run_id, seq=seq, ts=1000 + seq, message="x"))
        for seq in seqs
    ]


def attach(hub, run_id: str, job, *, replay_from_seq: int = 0, flushed_through_seq: int = 0):
    """Register a job socket and record the bounds its `hello` stated.

    Built from the real `hello()` helper rather than a hand-written dict, so
    a change to the frame's payload keys breaks this the way it would break a
    real connection.
    """
    hub.job_sockets.register(run_id, job)
    hub.job_sockets.record_bounds(
        run_id,
        hello(
            run_id,
            next_seq=999,
            replay_from_seq=replay_from_seq,
            flushed_through_seq=flushed_through_seq,
        ).payload,
    )
    return job


def with_warehouse(hub, rows: list[list]) -> FakeHttp:
    http = FakeHttp(statement_response(["seq", "ts", "type", "body"], rows))
    hub.sql = SqlClient("https://x", "wh", "tok", client=http)
    hub.repo = RunRepository(hub.sql, hub.tables)
    return http


def warehouse_log_row(seq: int) -> list:
    """The same log message the job would have served, as the warehouse has it."""
    body = (
        '{"message": "x", "level": "INFO", "source": "model", '
        '"phase": "run", "client_visible": true}'
    )
    return [seq, 1000 + seq, "log", body]


# --- the routing ------------------------------------------------------------


def test_a_gap_the_job_can_cover_is_served_from_memory_and_never_reaches_sql(app_and_hub):
    app, hub = app_and_hub()
    hub.sql = WarehouseTripwire()
    hub.repo = RunRepository(WarehouseTripwire(), TableSet())
    job = attach(hub, "r1", FakeJob(hub.job_sockets, "r1", messages=log_messages("r1", [4, 5, 6])))

    with TestClient(app) as client:
        body = client.get("/api/runs/r1/messages", params={"after_seq": 3}).json()

    assert body["source"] == "job"
    assert [m["seq"] for m in body["messages"]] == [4, 5, 6]
    assert body["count"] == 3 and body["next_after_seq"] == 6
    assert len(job.asked) == 1, "the job should have been asked exactly once"


def test_an_incomplete_answer_falls_through_to_the_warehouse(app_and_hub):
    """`complete: false` means the gap reached below the ring's floor — the
    job served what it had and the rest is the warehouse's to answer."""
    app, hub = app_and_hub()
    http = with_warehouse(hub, [warehouse_log_row(i) for i in (1, 2, 3)])
    job = attach(hub, "r1", FakeJob(hub.job_sockets, "r1", messages=[], complete=False))

    with TestClient(app) as client:
        body = client.get("/api/runs/r1/messages", params={"after_seq": 0}).json()

    assert job.asked, "the job is asked first even when it turns out it cannot cover it"
    assert body["source"] == "warehouse"
    assert [m["seq"] for m in body["messages"]] == [1, 2, 3]
    assert len(http.requests) == 1


def test_a_run_with_no_live_job_goes_straight_to_the_warehouse(app_and_hub):
    app, hub = app_and_hub()
    http = with_warehouse(hub, [warehouse_log_row(7)])

    with TestClient(app) as client:
        body = client.get("/api/runs/gone-home/messages", params={"after_seq": 6}).json()

    assert body["source"] == "warehouse" and body["count"] == 1
    assert len(http.requests) == 1


def test_a_gap_older_than_the_job_can_replay_is_not_even_asked(app_and_hub):
    """The bounds from `hello` are what decide this, and they decide it before
    a frame goes out: asking a job for a gap it has already evicted spends a
    round trip to be told to go to SQL anyway."""
    app, hub = app_and_hub()
    with_warehouse(hub, [warehouse_log_row(4)])
    job = attach(hub, "r1", FakeJob(hub.job_sockets, "r1"), replay_from_seq=100)

    with TestClient(app) as client:
        body = client.get("/api/runs/r1/messages", params={"after_seq": 3}).json()

    assert job.asked == [], "a gap below the ring's floor must not be sent to the job"
    assert body["source"] == "warehouse"


def test_a_gap_reaching_exactly_the_oldest_replayable_seq_is_still_the_jobs(app_and_hub):
    """`after_seq` is an exclusive lower bound, so asking for everything after
    the message one below the ring's oldest starts at the oldest — which the
    job has. Off by one the other way and every gap that reached exactly to
    the floor would wake the warehouse for nothing."""
    app, hub = app_and_hub()
    hub.repo = RunRepository(WarehouseTripwire(), TableSet())
    job = attach(
        hub,
        "r1",
        FakeJob(hub.job_sockets, "r1", messages=log_messages("r1", [100]), replay_from_seq=100),
        replay_from_seq=100,
    )

    with TestClient(app) as client:
        body = client.get("/api/runs/r1/messages", params={"after_seq": 99}).json()

    assert body["source"] == "job" and [m["seq"] for m in body["messages"]] == [100]
    assert len(job.asked) == 1


def test_a_job_that_never_answers_times_out_and_the_request_still_returns(app_and_hub, monkeypatch):
    """A wedged job must cost a bounded wait, not a hung browser request. The
    warehouse read it falls back to is slower than the wait, but it finishes."""
    app, hub = app_and_hub()
    monkeypatch.setattr(runs_routes, "BACKFILL_TIMEOUT_S", 0.05)
    http = with_warehouse(hub, [warehouse_log_row(1)])
    job = attach(hub, "r1", SilentJob())

    with TestClient(app) as client:
        resp = client.get("/api/runs/r1/messages", params={"after_seq": 0})

    assert resp.status_code == 200
    assert job.asked, "the job was asked; it just never answered"
    assert resp.json()["source"] == "warehouse"
    assert len(http.requests) == 1


def test_a_deploy_with_no_warehouse_still_serves_a_live_runs_gap_from_the_job(app_and_hub):
    """Lakebase and no SQL warehouse is a supported shape. A 503 for a gap the
    job just answered would make the read path required for a read it never
    performed."""
    app, hub = app_and_hub()
    attach(hub, "r1", FakeJob(hub.job_sockets, "r1", messages=log_messages("r1", [1])))

    with TestClient(app) as client:
        assert hub.repo is None, "no warehouse configured in this deployment"
        served = client.get("/api/runs/r1/messages", params={"after_seq": 0})
        # ...and the same request for a run with no live job still degrades
        # cleanly, rather than raising an AttributeError somewhere inside.
        unserved = client.get("/api/runs/r2/messages", params={"after_seq": 0})

    assert served.status_code == 200 and served.json()["source"] == "job"
    assert unserved.status_code == 503
    assert "no SQL warehouse configured" in unserved.json()["detail"]


def test_both_sources_answer_in_exactly_the_same_shape(app_and_hub):
    """A client cannot be made to care which one answered. Same keys, same
    message dicts, paged the same way — `source` is the only difference, and
    it is there so "did this wake the warehouse" is answerable without
    reading the app's log."""
    app, hub = app_and_hub()
    attach(hub, "r1", FakeJob(hub.job_sockets, "r1", messages=log_messages("r1", [4])))
    with TestClient(app) as client:
        from_job = client.get("/api/runs/r1/messages", params={"after_seq": 3}).json()

    app, hub = app_and_hub()
    with_warehouse(hub, [warehouse_log_row(4)])
    with TestClient(app) as client:
        from_warehouse = client.get("/api/runs/r1/messages", params={"after_seq": 3}).json()

    assert from_job.keys() == from_warehouse.keys()
    assert from_job["messages"] == from_warehouse["messages"], (
        "one envelope shape, whichever side of the boundary it came from"
    )
    assert {k: v for k, v in from_job.items() if k != "source"} == {
        k: v for k, v in from_warehouse.items() if k != "source"
    }
    assert (from_job["source"], from_warehouse["source"]) == ("job", "warehouse")


def test_the_app_never_asks_a_job_for_more_than_it_will_send(app_and_hub):
    """`more` is computed against what was asked for, so asking for more than
    the job's own cap would read a truncated page as "that is all there is"
    and stop a client paging in the middle of its gap."""
    from job.bus import DEFAULT_BACKFILL_LIMIT

    assert runs_routes.JOB_REPLY_LIMIT <= DEFAULT_BACKFILL_LIMIT

    app, hub = app_and_hub()
    job = attach(hub, "r1", FakeJob(hub.job_sockets, "r1", messages=log_messages("r1", [1])))
    with TestClient(app) as client:
        # No `limit`, so the route falls back to `backfill_page_size` (5000) —
        # far more than a job will ever put in one reply.
        client.get("/api/runs/r1/messages", params={"after_seq": 0})

    assert job.asked[0].payload["limit"] == runs_routes.JOB_REPLY_LIMIT


def test_a_full_page_from_the_job_tells_the_client_to_ask_again(app_and_hub):
    app, hub = app_and_hub()
    attach(hub, "r1", FakeJob(hub.job_sockets, "r1", messages=log_messages("r1", [1, 2, 3, 4])))

    with TestClient(app) as client:
        body = client.get("/api/runs/r1/messages", params={"after_seq": 0, "limit": 3}).json()

    assert body["more"] is True and body["next_after_seq"] == 3
    assert body["source"] == "job"


# --- JobConnections, up close ----------------------------------------------


async def test_bounds_a_job_never_stated_are_not_invented():
    """A missing bound is not a bound of zero. Defaulting the floor to 0 would
    read as "this job can replay anything", so every gap would be sent to a
    socket that cannot serve it and each one would cost a timeout before the
    warehouse read that should have happened immediately."""
    conns = JobConnections()
    conns.register("r1", SilentJob())

    conns.record_bounds("r1", {"next_seq": 12})
    assert conns.bounds("r1") is None
    assert conns.can_serve("r1", 0) is False

    conns.record_bounds("r1", {"replay_from_seq": None, "flushed_through_seq": 4})
    assert conns.bounds("r1") is None


async def test_bounds_are_refreshed_by_every_reply_not_only_by_hello():
    """A run that never reconnects would otherwise be judged for hours by the
    floor it stated at connect time, while its ring moved on underneath."""
    conns = JobConnections()
    conns.register("r1", SilentJob())
    conns.record_bounds("r1", hello("r1", replay_from_seq=0, flushed_through_seq=0).payload)
    assert conns.can_serve("r1", 5) is True

    conns.resolve_backfill(
        "r1",
        backfill_result(
            "r1",
            after_seq=5,
            messages=[],
            complete=True,
            replay_from_seq=900,
            flushed_through_seq=880,
        ).payload,
    )
    assert conns.bounds("r1").replay_from_seq == 900
    assert conns.can_serve("r1", 5) is False, "that gap is the warehouse's now"


async def test_a_second_backfill_for_one_run_goes_to_sql_rather_than_stealing_the_reply():
    """Two waiters on one future would both be woken by whichever reply landed
    first — a page computed for someone else's cursor, which is silently wrong
    messages where the warehouse would have given right ones. Refusing the
    overlap costs a rare warehouse read instead."""
    conns = JobConnections()
    conns.register("r1", SilentJob())

    first = asyncio.create_task(conns.backfill("r1", after_seq=0, timeout_s=1.0))
    await asyncio.sleep(0.01)

    assert await conns.backfill("r1", after_seq=500, timeout_s=1.0) is None

    conns.resolve_backfill(
        "r1",
        backfill_result(
            "r1",
            after_seq=0,
            messages=[],
            complete=True,
            replay_from_seq=0,
            flushed_through_seq=0,
        ).payload,
    )
    answer = await first
    assert answer is not None and answer["after_seq"] == 0


async def test_a_reply_to_a_request_that_timed_out_cannot_satisfy_the_next_one():
    conns = JobConnections()
    conns.register("r1", SilentJob())
    assert await conns.backfill("r1", after_seq=0, timeout_s=0.02) is None

    later = asyncio.create_task(conns.backfill("r1", after_seq=90, timeout_s=0.1))
    await asyncio.sleep(0.01)

    stale = backfill_result(
        "r1",
        after_seq=0,
        messages=log_messages("r1", [1, 2]),
        complete=True,
        replay_from_seq=0,
        flushed_through_seq=0,
    ).payload
    assert conns.resolve_backfill("r1", stale) is False
    assert await later is None, "a page for seq 0 must not be handed to a request for seq 90"


async def test_a_timed_out_backfill_leaves_nothing_behind_to_wedge_the_next_one():
    """A leaked pending entry would refuse every later backfill for the run,
    for the life of the socket — a permanent silent fallback to SQL."""
    conns = JobConnections()
    conns.register("r1", SilentJob())
    assert await conns.backfill("r1", after_seq=0, timeout_s=0.02) is None

    conns.register("r1", FakeJob(conns, "r1", messages=log_messages("r1", [1])))
    answer = await conns.backfill("r1", after_seq=0, timeout_s=1.0)
    assert answer is not None and [m["seq"] for m in answer["messages"]] == [1]


async def test_a_job_that_disconnects_mid_backfill_releases_the_waiter_at_once():
    """The answer is never coming; holding the browser's request for the rest
    of the timeout only delays the warehouse read it now needs."""
    conns = JobConnections()
    conns.register("r1", SilentJob())

    waiting = asyncio.create_task(conns.backfill("r1", after_seq=0, timeout_s=30))
    await asyncio.sleep(0.01)
    conns.unregister("r1")

    assert await asyncio.wait_for(waiting, timeout=1) is None


async def test_a_reconnecting_job_is_not_judged_by_the_old_sockets_floor():
    """Bounds belong to the connection that stated them. A job re-attaches and
    says `hello` again immediately; in that window an inherited floor would
    route a gap to a socket that has not claimed it can serve it."""
    conns = JobConnections()
    conns.register("r1", SilentJob())
    conns.record_bounds("r1", hello("r1", replay_from_seq=0, flushed_through_seq=0).payload)

    conns.unregister("r1")
    conns.register("r1", SilentJob())
    assert conns.bounds("r1") is None
    assert conns.can_serve("r1", 5) is False


async def test_a_backfill_to_a_run_with_no_socket_is_refused_without_a_wait():
    conns = JobConnections()
    assert await conns.backfill("nobody", after_seq=0, timeout_s=30) is None


async def test_a_send_that_fails_gives_up_on_the_job_rather_than_waiting_it_out():
    class BrokenJob:
        async def send_bytes(self, raw: bytes) -> None:
            raise RuntimeError("socket is gone")

    conns = JobConnections()
    conns.register("r1", BrokenJob())
    assert await conns.backfill("r1", after_seq=0, timeout_s=30) is None
    assert conns.is_connected("r1") is False, "a failed send drops the socket"


def test_the_backfill_frame_the_app_sends_is_the_one_the_job_parses(app_and_hub):
    """The app builds its request with `shared.protocol.backfill`, so what
    goes out is decodable by the job's own `unpack_frame` — the two sides
    share the module, and this is what notices if the app ever hand-rolls it."""
    from job.shared.protocol import ControlKind as JobControlKind
    from job.shared.protocol import unpack_frame as job_unpack

    app, hub = app_and_hub()
    job = attach(hub, "r1", FakeJob(hub.job_sockets, "r1", messages=log_messages("r1", [1])))
    with TestClient(app) as client:
        client.get("/api/runs/r1/messages", params={"after_seq": 0})

    decoded = job_unpack(pack_frame(job.asked[0]))
    assert decoded.kind is JobControlKind.BACKFILL
    assert decoded.payload["after_seq"] == 0
