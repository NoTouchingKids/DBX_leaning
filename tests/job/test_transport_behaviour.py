"""Characterisation tests for the job's transport — pinned at the observable
contract, not at ``job/buffer.py``, ``job/record.py`` or ``job/bus.py``'s
internals.

Those three modules are about to be replaced by a single message stream with
cursors. Nothing here asserts on ``_q``, ``_replay``, ``_pending``, or any
other structure that is going away. Every test drives a real ``JobHarness``
(occasionally an ``Emitter`` alone) and asserts on what a model producing
envelope messages actually causes to happen: what lands in the durable
writer, what reaches a socket, what a BACKFILL answers on the wire, and what
the run's own outcome says. A correct rewrite of the three modules above
should pass this file completely unmodified — that is the point of it.

Where a test restates something ``tests/job/test_runner.py`` or
``tests/job/test_flush_rules.py`` already covers at a lower level, its
docstring says so and says why it is worth restating here too — usually
because this file checks the same property from the wire or the durable
writer's own persisted output, rather than from the harness's internal
fields.

Reuses ``FakeModel``/``BlockingModel``/``FakeSocket``/``connector``/``until``
from ``conftest.py``; adds no fixtures of its own there — anything else a
test needs is built locally, the same way ``test_runner.py`` builds one-off
model and writer doubles inline. Envelopes and control frames are built
through ``job.shared.*``, never ``shared.*``: byte-identical source, distinct
types (see ``CLAUDE.md``) — ``MessageType.LOG is MessageType.LOG`` is False
across them.
"""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from job.bus import WebSocketBus
from job.delta import JsonlWriter
from job.lakebase import LakebaseStatus
from job.loader import describe_object
from job.record import RunRecord
from job.runner import JobHarness
from job.shared.envelope import LogMessage, RunStatus, StatusMessage
from job.shared.protocol import ControlKind, backfill, pack_frame

from .conftest import BlockingModel, FakeSocket, connector, until


def rows(writer: JsonlWriter, table: str) -> list[dict]:
    return writer.read_all(f"main.dbx_leaning.{table}")


def bus_over(ws: FakeSocket, run_id: str, **kw) -> WebSocketBus:
    """``record=`` is required; the harness swaps in its own on the way in
    (see ``JobHarness._build_bus``), so what a BACKFILL answers with is
    always the run's real replay ring, not this placeholder."""
    return WebSocketBus("ws://x/ws", run_id, record=RunRecord(run_id), connect=connector(ws), **kw)


# --- durability: never drops, no matter what the live path does -----------


async def test_the_durable_record_stays_gap_free_and_complete_no_matter_what_the_live_path_drops(
    cfg, writer
):
    """Pins three things at once, deliberately, because they are one story:
    the durable path is the floor and the live path is not, no matter which
    message type is involved or how badly the socket falls behind.

    1. Every message the model emits reaches the durable writer — including a
       ``client_visible=False`` log, which never goes out live at all, and
       every progress point a deliberately tiny live queue sheds under
       backpressure. If a rewrite let durability share fate with the live
       queue, a run watched live and the same run read back from Delta
       afterwards could disagree about what happened during it.
    2. The live path is *allowed* to drop under pressure, and says so
       (``RunOutcome.live_dropped``), and the run still succeeds — a
       backpressured socket must never fail a run or block a model's worker
       thread.
    3. ``seq`` in the durable record stays gap-free and strictly increasing
       across every message type (log, progress, status, result) even though
       the live copy is missing exactly what it dropped. This restates
       ``test_runner.py::test_seq_is_gap_free_across_the_whole_run`` — which
       covers the no-socket-at-all case — under live backpressure instead,
       on purpose: gap-freeness is a property of the durable write path, and
       this is the case where a wrong implementation would most plausibly
       let a live-path shortcut leak into it (e.g. renumbering around a
       dropped message, or only durably writing what also went out live).
    """
    steps = 150

    class FloodingModel:
        """Emits fast enough, for long enough real wall-clock time (the sleep
        is what makes this deterministic across hardware speeds, not raw
        iteration count), that a one-slot live queue cannot possibly keep up.
        """

        def __init__(self) -> None:
            self.emit = None
            self.should_cancel = None

        def run(self) -> None:
            self.emit(
                "log",
                message="raw solver chatter",
                source="model",
                phase="input",
                client_visible=False,
            )
            for i in range(steps):
                self.emit(
                    "progress",
                    elapsed_seconds=float(i),
                    percent_complete=100.0 * (i + 1) / steps,
                )
                time.sleep(0.001)

        def results(self) -> list[dict]:
            return []

    model = FloodingModel()
    conf = cfg(model_spec="x:FloodingModel")
    ws = FakeSocket(send_delay_s=0.03)
    bus = bus_over(ws, conf.run_id, queue_max=1, batch_max=1)

    outcome = await JobHarness(
        conf, writer=writer, bus=bus, handle=describe_object(model, "flood")
    ).run()

    assert outcome.status is RunStatus.SUCCEEDED
    assert outcome.live_dropped > 0, "the setup is meant to overrun a one-slot live queue"

    live_seqs = {m.seq for m in ws.messages()}
    assert len(live_seqs) < outcome.seq_issued, (
        "nothing was actually shed live; this proves nothing"
    )

    durable_seqs = sorted(
        r["seq"]
        for table in ("run_logs", "run_progress", "run_events", "run_results_meta")
        for r in rows(writer, table)
    )
    assert durable_seqs == list(range(outcome.seq_issued)), (
        "the durable record must be gap-free and complete regardless of what the live path lost"
    )

    invisible = [r for r in rows(writer, "run_logs") if not r["client_visible"]]
    assert len(invisible) == 1, "the client_visible=False log must still be durable"
    live_texts = [m.message for m in ws.messages() if isinstance(m, LogMessage)]
    assert "raw solver chatter" not in live_texts, "it must never have reached the socket"


async def test_a_run_that_raises_after_emitting_a_result_keeps_that_result_durably(cfg, writer):
    """Results are not best-effort, and this is the half of that rule
    ``test_runner.py`` does not already cover:
    ``test_results_are_written_even_when_the_run_is_cancelled`` pins the
    cancellation case, but a model that raises partway through — after it has
    already produced something — must keep that something too. If a rewrite
    only special-cased CANCELLED and treated any other exception as "discard
    everything the run produced", a model that fails on group 40 of 48 in a
    chunked run would silently lose the 39 good chunks it already wrote,
    right when a partial answer matters most.
    """

    class PartialThenExplode:
        results_table = "results_partial"

        def __init__(self) -> None:
            self.emit = None
            self.should_cancel = None

        def run(self) -> None:
            self.emit("result", rows=[{"a": 1}, {"a": 2}], final=False)
            raise ValueError("boom on the second chunk")

        def results(self) -> list[dict]:
            # Must never be reached: the model already streamed its own
            # result, so the harness must not also call this accessor.
            return [{"should_not_appear": True}]

    conf = cfg(model_spec="x:PartialThenExplode")
    outcome = await JobHarness(
        conf, writer=writer, handle=describe_object(PartialThenExplode(), "partial")
    ).run()

    assert outcome.status is RunStatus.FAILED
    assert "ValueError: boom" in outcome.detail
    assert outcome.result_rows == 2

    written = rows(writer, "results_partial")
    assert len(written) == 2 and all("should_not_appear" not in r for r in written)
    assert [r["a"] for r in written] == [1, 2]

    meta = rows(writer, "run_results_meta")
    assert len(meta) == 1 and meta[0]["row_count"] == 2


# --- client_visible=False: a live-send filter, and nothing else -----------


async def test_client_invisible_logs_are_durable_but_reach_neither_the_socket_nor_a_backfill_reply(
    cfg, writer
):
    """``client_visible=False`` is a live-SEND filter only: durable yes,
    socket no. Checked on *both* of the paths a client can actually receive
    messages from — the live push and a BACKFILL reply — because
    ``app/server/repository.py`` withholds the durable backfill on the same
    column, and the two sources disagreeing about what a client is shown is
    exactly the failure the one-envelope design exists to prevent
    (``docs/architecture.md``, "Why the message envelope is one shape"). A
    rewrite that filtered only the live push and forgot the BACKFILL reply
    would leak raw solver chatter to a client the instant it reconnected.
    """

    class EmitsThenBlocks:
        results_table = "results_visibility"

        def __init__(self) -> None:
            self.emit = None
            self.should_cancel = None
            self.ready = threading.Event()

        def run(self) -> None:
            self.emit("log", message="shown", source="model", phase="input", client_visible=True)
            self.emit("log", message="hidden", source="model", phase="input", client_visible=False)
            self.ready.set()
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if self.should_cancel():
                    return
                time.sleep(0.01)
            raise AssertionError("cancellation was never observed")

        def results(self) -> list[dict]:
            return []

    model = EmitsThenBlocks()
    conf = cfg(model_spec="x:EmitsThenBlocks")
    ws = FakeSocket()
    bus = bus_over(ws, conf.run_id)
    harness = JobHarness(conf, writer=writer, bus=bus, handle=describe_object(model, "vis"))

    async def backfill_then_stop() -> None:
        await until(lambda: bus.is_connected)
        await asyncio.to_thread(model.ready.wait, 2.0)
        ws.push(pack_frame(backfill(conf.run_id, after_seq=-1)))
        await until(lambda: bool(ws.control(ControlKind.BACKFILL_RESULT)))
        harness.token.cancel("stop")

    asyncio.create_task(backfill_then_stop())
    outcome = await harness.run()

    assert outcome.status is RunStatus.CANCELLED

    log_rows = rows(writer, "run_logs")
    assert {r["message"] for r in log_rows} >= {"shown", "hidden"}, "both must be durable"
    hidden = next(r for r in log_rows if r["message"] == "hidden")
    assert hidden["client_visible"] is False

    live_texts = [m.message for m in ws.messages() if isinstance(m, LogMessage)]
    assert "shown" in live_texts and "hidden" not in live_texts

    payload = ws.control(ControlKind.BACKFILL_RESULT)[0].payload
    backfilled_texts = [m["message"] for m in payload["messages"] if m["type"] == "log"]
    assert "shown" in backfilled_texts and "hidden" not in backfilled_texts


# --- backfill: complete above the floor, honest about it below ------------


async def test_a_backfill_above_the_floor_is_complete_below_it_is_not_and_a_limit_only_truncates(
    cfg, writer
):
    """All three BACKFILL clauses, read straight off the wire against a ring
    that has actually been made to overflow by driving a real run — rather
    than constructing a ``RunRecord`` directly the way
    ``tests/job/test_record.py`` does. A client that trusted a false
    ``complete: true`` would silently render a run with holes in it; a
    client told ``complete: false`` when the job actually held everything
    would make an unnecessary (uptime-costing) warehouse read on every
    reconnect; and a paging client that stopped early because a truncated
    page came back marked complete would truncate every gap wider than the
    server's own limit.
    """

    class LogsUntilBlocked:
        def __init__(self, count: int) -> None:
            self.count = count
            self.emit = None
            self.should_cancel = None
            self.emitted = threading.Event()

        def run(self) -> None:
            for i in range(self.count):
                self.emit("log", message=f"m{i}", source="model", phase="run")
            self.emitted.set()
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if self.should_cancel():
                    return
                time.sleep(0.01)
            raise AssertionError("cancellation was never observed")

    model = LogsUntilBlocked(count=20)
    # A ring this small next to 20+ model messages is guaranteed to overflow.
    conf = cfg(model_spec="x:LogsUntilBlocked", live_queue_max=5)
    ws = FakeSocket()
    bus = bus_over(ws, conf.run_id)
    harness = JobHarness(conf, writer=writer, bus=bus, handle=describe_object(model, "ring"))

    async def ask(after_seq: int, **kw) -> dict:
        before = len(ws.control(ControlKind.BACKFILL_RESULT))
        ws.push(pack_frame(backfill(conf.run_id, after_seq=after_seq, **kw)))
        await until(lambda: len(ws.control(ControlKind.BACKFILL_RESULT)) > before)
        return ws.control(ControlKind.BACKFILL_RESULT)[-1].payload

    async def probe_then_stop() -> None:
        await until(lambda: bus.is_connected)
        await asyncio.to_thread(model.emitted.wait, 2.0)

        far = await ask(after_seq=-1)
        assert far["complete"] is False, "far more was asked for than a 5-slot ring can hold"
        floor = far["replay_from_seq"]
        assert floor > 0, "the ring must actually have overflowed for this test to mean anything"

        at_floor = await ask(after_seq=floor)
        assert at_floor["complete"] is True, "asking from exactly the retained floor needs no SQL"
        assert len(at_floor["messages"]) >= 2, "need headroom above the floor for the next check"

        truncated = await ask(after_seq=floor, limit=2)
        assert len(truncated["messages"]) == 2
        assert truncated["complete"] is True, "a truncated page is still complete as far as it goes"

        harness.token.cancel("stop")

    asyncio.create_task(probe_then_stop())
    outcome = await harness.run()

    assert outcome.status is RunStatus.CANCELLED
    assert outcome.backfills_served == 3


# --- the two advertised bounds ---------------------------------------------


async def test_flushed_through_seq_on_the_wire_stays_below_a_table_that_has_not_flushed_yet(
    cfg, writer
):
    """``flushed_through_seq`` is one below the LOWEST pending seq across
    every table, never "the highest seq anything has written" — restated at
    the observable level from
    ``test_flush_rules.py::test_the_durable_high_water_mark_stops_below_the_lowest_pending_row``,
    which pins the same arithmetic directly against ``DurableBuffer``. This
    version asks the actual wire, because that is the number a reconnecting
    client acts on: if it drifted to "highest seq written", a client would
    be told the warehouse can serve a row that has not actually reached it,
    and go fetch nothing.

    The run's own opening status message (``run_events``, one small row) is
    the table this deliberately starves of its own flush trigger, while a
    flood of log rows (``run_logs``, a different table) crosses the size
    bound almost immediately — proving the bound tracks the *table*, not
    whichever one happens to be biggest or newest.
    """

    class NoisyLogger:
        results_table = "results_noisy_logger"

        def __init__(self) -> None:
            self.emit = None
            self.should_cancel = None
            self.emitted = threading.Event()

        def run(self) -> None:
            for _ in range(20):
                self.emit("log", message="x" * 200, source="model", phase="run")
            self.emitted.set()
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if self.should_cancel():
                    return
                time.sleep(0.01)
            raise AssertionError("cancellation was never observed")

        def results(self) -> list[dict]:
            return []

    model = NoisyLogger()
    conf = cfg(
        model_spec="x:NoisyLogger",
        flush_max_bytes=1000,
        flush_max_age_s=1.0,
        flush_tick_s=0.02,
    )
    ws = FakeSocket()
    bus = bus_over(ws, conf.run_id)
    harness = JobHarness(conf, writer=writer, bus=bus, handle=describe_object(model, "noisy"))

    async def probe_then_stop() -> None:
        await until(lambda: bus.is_connected)
        await asyncio.to_thread(model.emitted.wait, 2.0)
        # Comfortably more than one flush tick (0.02s), comfortably less than
        # the age bound (1.0s) that would eventually clear run_events too.
        await asyncio.sleep(0.2)

        assert len(rows(writer, "run_logs")) >= 21, (
            "the size bound must have already flushed the logs"
        )
        assert rows(writer, "run_events") == [], "the opening status row must still be unflushed"

        ws.push(pack_frame(backfill(conf.run_id, after_seq=-1)))
        await until(lambda: bool(ws.control(ControlKind.BACKFILL_RESULT)))
        payload = ws.control(ControlKind.BACKFILL_RESULT)[0].payload

        assert payload["flushed_through_seq"] == -1, (
            "many higher-seq log rows are already durable (confirmed above), but the "
            "run's very first row is still only in memory in a different table, so "
            "nothing may be advertised as safe yet"
        )
        harness.token.cancel("stop")

    asyncio.create_task(probe_then_stop())
    outcome = await harness.run()

    assert outcome.status is RunStatus.CANCELLED


# --- flush triggers: size, age, end-of-run, and surviving a failure -------


@pytest.mark.parametrize(
    ("flush_max_bytes", "flush_max_age_s"),
    [(1, 999.0), (10**9, 0.02)],
    ids=["size", "age"],
)
async def test_a_flush_fires_mid_run_under_either_trigger_not_only_at_teardown(
    cfg, writer, flush_max_bytes, flush_max_age_s
):
    """Restates, at the observable level, what
    ``test_runner.py::test_the_durable_high_water_mark_advances_while_the_run_is_still_going``
    and ``test_flush_rules.py``'s own size/age unit tests already pin
    directly against the buffer — worth restating here because a flush
    happening independently of end-of-run is a property of the whole
    harness's wiring (the periodic flusher thread), not just of
    ``DurableBuffer.due()`` in isolation. Breaking this would mean a
    long-running model's telemetry sits entirely in memory until it
    finishes, which is the opposite of what makes Delta "the floor, not a
    fallback tier" for a run nobody happens to be watching live.
    """
    model = BlockingModel({"poll_s": 0.005})
    conf = cfg(
        model_spec="tests.job.conftest:BlockingModel",
        flush_max_bytes=flush_max_bytes,
        flush_max_age_s=flush_max_age_s,
        flush_tick_s=0.01,
    )
    harness = JobHarness(conf, writer=writer, handle=describe_object(model, "blocking"))

    async def observe_then_stop() -> None:
        await asyncio.to_thread(model.started.wait, 2.0)
        assert await until(lambda: len(rows(writer, "run_events")) >= 1), (
            "neither trigger produced a flush before the run was stopped"
        )
        harness.token.cancel("stop")

    asyncio.create_task(observe_then_stop())
    outcome = await harness.run()

    assert outcome.status is RunStatus.CANCELLED
    assert len(rows(writer, "run_events")) >= 1


async def test_end_of_run_flushes_everything_even_when_neither_size_nor_age_ever_crossed(
    cfg, writer
):
    """The third trigger. With both bounds set so high that nothing could
    possibly cross them mid-run, the only way any row can land is the
    guaranteed flush at teardown — so a clean pass here means end-of-run
    flushing works with zero help from the periodic ticker, which is exactly
    the case a short, fast-finishing run relies on.
    """
    conf = cfg(model_config={"steps": 3}, flush_max_bytes=10**9, flush_max_age_s=10**9)
    outcome = await JobHarness(conf, writer=writer).run()

    assert outcome.status is RunStatus.SUCCEEDED
    assert outcome.unflushed_rows == 0
    assert len(rows(writer, "run_progress")) == 3, "nothing could have flushed except at teardown"


async def test_a_failed_flush_is_retried_and_the_run_ends_up_with_everything_in_order(cfg, writer):
    """Restates ``test_flush_rules.py::test_a_failed_write_requeues_rather_than_losing_rows``
    and ``test_restored_rows_keep_their_place_ahead_of_newer_ones`` on
    purpose, through a real end-to-end run rather than a ``DurableSink``
    exercised directly: this is what proves the harness actually wires a
    real writer failure through to a full, correctly ordered durable record
    while keeping the run's declared status honest, not just that the buffer
    class does the right thing in isolation. A rewrite could get the buffer
    right and still lose this if, say, retried rows were appended instead of
    requeued at the front, silently reordering a run's telemetry.
    """

    class FlakyOnceWriter:
        """Fails the first write to one specific table, then behaves — for
        that table and every other, on every call after. Narrower than
        ``test_flush_rules.py``'s own flaky writer on purpose: it does not
        matter here which table happens to flush first, only that a
        real failure survives the whole harness with nothing lost or
        reordered."""

        name = "flaky-once"

        def __init__(self, fail_table: str) -> None:
            self._fail_table = fail_table
            self._failed_once = False
            self.attempts = 0
            self.rows: dict[str, list[dict]] = {}

        def write_batch(self, table: str, rows: list[dict]) -> int:
            self.attempts += 1
            if table == self._fail_table and not self._failed_once:
                self._failed_once = True
                raise RuntimeError("delta unavailable")
            self.rows.setdefault(table, []).extend(rows)
            return len(rows)

        def close(self) -> None: ...

    flaky = FlakyOnceWriter(fail_table="main.dbx_leaning.run_progress")
    conf = cfg(
        model_config={"steps": 8, "step_sleep": 0.02},
        flush_max_bytes=1,
        flush_max_age_s=0.05,
        flush_tick_s=0.01,
    )
    outcome = await JobHarness(conf, writer=flaky).run()

    assert outcome.status is RunStatus.SUCCEEDED
    assert outcome.write_failures == 1, (
        "the induced failure must actually have been hit, exactly once"
    )

    written = flaky.rows["main.dbx_leaning.run_progress"]
    assert [r["elapsed_seconds"] for r in written] == [float(i) for i in range(8)], (
        "a retried write must not lose, duplicate, or reorder what was already buffered"
    )


# --- the terminal status: always out, and always last ----------------------


async def test_a_run_that_outruns_its_socket_still_delivers_its_terminal_status_as_the_last_frame(
    cfg, writer
):
    """Restates
    ``test_runner.py::test_a_run_that_outruns_its_socket_still_delivers_the_terminal_status``
    on purpose — this is the single most consequential live-path guarantee
    the harness makes, and a rewrite that broke only its "arrives LAST"
    clause could still pass every other test in this file. Extended with an
    assertion the original does not make: the terminal status is not merely
    present somewhere in the stream, it is the literal final frame this
    socket ever received. A client that stops reading at the first terminal
    status it sees (the browser does exactly this) must never see one early
    with more of the run's own history trailing after it, and must never
    fail to see one at all because a slow socket was still draining the
    middle of the run when the process exited.

    Needs a socket whose ``send`` genuinely awaits — see ``FakeSocket``'s own
    docstring in ``conftest.py`` for why a fake that returns immediately
    cannot exercise this at all.
    """
    ws = FakeSocket(send_delay_s=0.001)
    conf = cfg(model_config={"steps": 300})
    bus = bus_over(ws, conf.run_id)

    outcome = await JobHarness(conf, writer=writer, bus=bus).run()

    assert outcome.status is RunStatus.SUCCEEDED
    assert outcome.live_undrained == 0, "the drain gave up with the run still queued"
    assert outcome.live_sent == outcome.seq_issued, "the live stream lost messages"

    messages = ws.messages()
    assert messages, "nothing reached the socket at all"
    last = messages[-1]
    assert isinstance(last, StatusMessage) and last.status is RunStatus.SUCCEEDED
    assert last.seq == outcome.seq_issued - 1, "the terminal status must be the final frame sent"


# --- SUCCEEDED must be structurally impossible over a lost write ----------


async def test_a_lost_durable_write_is_reported_as_failed_on_every_channel_the_job_speaks_on(
    cfg, writer
):
    """Restates ``test_runner.py::test_succeeded_is_impossible_over_a_lost_result_write``
    on purpose, and extends it: a rewrite could satisfy that check by fixing
    up only the ``RunOutcome`` object a test inspects, while still leaking
    SUCCEEDED out through the terminal status message or the Lakebase
    report — two other, independently observable places a run's outcome
    travels. If any of them ever disagreed, a client watching live, a client
    reading ``run_status`` afterwards, and a client reading ``run_events``
    afterwards could each walk away with a different answer to "did this run
    succeed" for the very same run — precisely what CLAUDE.md calls out as
    worse than an honest failure.
    """

    class DeadWriter:
        name = "dead"

        def write_batch(self, table: str, rows: list[dict]) -> int:
            raise RuntimeError("unity catalog unreachable")

        def close(self) -> None: ...

    posted: list[str] = []

    class Client:
        async def post(self, url, json=None, headers=None):
            posted.append(json["parameters"][3])
            return SimpleNamespace(status_code=200, text="")

        async def aclose(self) -> None: ...

    reporter = LakebaseStatus("https://db/statements", client=Client())
    conf = cfg(model_config={"steps": 2})

    outcome = await JobHarness(conf, writer=DeadWriter(), status_reporter=reporter).run()

    assert outcome.status is RunStatus.FAILED
    assert "Refusing to report SUCCEEDED" in outcome.detail

    assert posted[0] == "RUNNING"
    assert posted[-1] == "FAILED", "the Lakebase report must not say SUCCEEDED either"
