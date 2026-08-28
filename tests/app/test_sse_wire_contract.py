"""Does ``EventSource``'s native resume actually work against *this* server?

ADR-001 action item 3. The browser now subscribes per named event
(``addEventListener('progress', ...)``) instead of one ``onmessage``, and
``app/server/routes/stream.py`` is written on the assumption that ``Last-Event-ID``
resume works unchanged across named events. That assumption is correct *per
the SSE spec*. "Correct per spec" is not "verified against this server", and
this repo has three bugs in its history that were right in every offline test
and wrong on first contact.

So these tests do not call ``stream_run()`` as a coroutine the way
``test_stream.py`` does. They start the real app under a real uvicorn on a
real socket and read the literal bytes off a raw TCP connection, then feed
them to an event-stream parser written the way a browser's is. Two failure
modes only this can catch:

- Framing. A missing blank line, a data field with an embedded newline, a
  chunk boundary through a multi-byte character — every one of those passes a
  substring assertion and produces a stream no ``EventSource`` can read.
- The header. ``Last-Event-ID`` has to survive ASGI, uvicorn's header
  handling and Starlette's case folding to reach ``request.headers``. Calling
  the endpoint function directly asserts the endpoint, not the stack.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import json
import re
from dataclasses import dataclass, field

import pytest
import uvicorn

from server.main import create_app
from server.repository import RunRepository
from shared.envelope import make_message

from .test_results import ScriptedSql

# ---------------------------------------------------------------------------
# A browser's event-stream parser, near enough
# ---------------------------------------------------------------------------


@dataclass
class SSEEvent:
    id: str
    type: str
    data: str


class EventSourceParser:
    """WHATWG event-stream parsing, implemented the way a browser does it.

    Written out rather than asserted with substrings on purpose. ``"id: 4" in
    body`` is true of a stream with no blank lines in it at all, which is a
    stream a browser shows nothing from. Only a parser that dispatches on a
    genuine blank line can tell those apart.

    It also carries ``last_event_id`` forward across events exactly as the
    spec says (set when the ``id`` field is *parsed*, not when the event is
    dispatched, and never reset between events) — because that value is
    precisely what the browser would put in the header on reconnect, and the
    tests below reconnect with whatever this computed rather than with a
    number hard-coded by someone reading the source.
    """

    def __init__(self) -> None:
        self.events: list[SSEEvent] = []
        self.comments: list[str] = []
        self.retry: int | None = None
        self.last_event_id = ""
        self._decoder = codecs.getincrementaldecoder("utf-8")()
        self._buffer = ""
        self._data: list[str] = []
        self._type = ""

    def feed(self, chunk: bytes) -> None:
        self._buffer += self._decoder.decode(chunk)
        while True:
            match = re.search(r"\r\n|\n|\r", self._buffer)
            if match is None:
                return
            # A trailing lone CR may yet turn out to be the first half of a
            # CRLF, so it is not a line terminator until more bytes say so.
            if match.group() == "\r" and match.end() == len(self._buffer):
                return
            line, self._buffer = self._buffer[: match.start()], self._buffer[match.end() :]
            self._line(line)

    def _line(self, line: str) -> None:
        if line == "":
            self._dispatch()
        elif line.startswith(":"):
            self.comments.append(line[1:].lstrip())
        else:
            field_name, _, value = line.partition(":")
            if value.startswith(" "):
                value = value[1:]
            if field_name == "data":
                self._data.append(value)
            elif field_name == "event":
                self._type = value
            elif field_name == "id" and "\0" not in value:
                self.last_event_id = value
            elif field_name == "retry" and value.isdigit():
                self.retry = int(value)

    def _dispatch(self) -> None:
        data, self._data, event_type, self._type = "\n".join(self._data), [], self._type, ""
        if not data:
            return  # a block with no data fires nothing in a browser
        self.events.append(SSEEvent(id=self.last_event_id, type=event_type, data=data))

    # -- conveniences the assertions read better through -------------------

    @property
    def ids(self) -> list[str]:
        return [e.id for e in self.events]

    @property
    def types(self) -> list[str]:
        return [e.type for e in self.events]

    def payloads(self) -> list[dict]:
        return [json.loads(e.data) for e in self.events]


# ---------------------------------------------------------------------------
# A raw HTTP/1.1 client, so the bytes under test are the bytes on the wire
# ---------------------------------------------------------------------------


class WireConnection:
    """Just enough HTTP/1.1 to read a chunked stream, and nothing more.

    Not httpx: a client library normalises exactly the framing this file
    exists to check. Each HTTP chunk is kept whole, because one chunk is one
    ``yield`` from the endpoint's generator, which lets a test assert that a
    single SSE frame is never split or coalesced on its way out.
    """

    def __init__(self, reader, writer) -> None:
        self._reader = reader
        self._writer = writer
        self.status = 0
        self.headers: dict[str, str] = {}
        self.chunks: list[bytes] = []
        self.parser = EventSourceParser()

    @classmethod
    async def open(cls, port: int, path: str, headers: dict[str, str] | None = None):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        conn = cls(reader, writer)
        lines = [
            f"GET {path} HTTP/1.1",
            f"Host: 127.0.0.1:{port}",
            "Accept: text/event-stream",
            *(f"{k}: {v}" for k, v in (headers or {}).items()),
        ]
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
        await writer.drain()
        await conn._read_head()
        return conn

    async def _read_head(self) -> None:
        head = await asyncio.wait_for(self._reader.readuntil(b"\r\n\r\n"), 5)
        status_line, *header_lines = head.decode().split("\r\n")
        self.status = int(status_line.split(" ")[1])
        for line in header_lines:
            if ":" in line:
                name, _, value = line.partition(":")
                self.headers[name.strip().lower()] = value.strip()

    async def next_chunk(self, within: float = 1.0) -> bytes | None:
        """One decoded HTTP chunk, or None if nothing arrived in time."""
        try:
            size_line = await asyncio.wait_for(self._reader.readuntil(b"\r\n"), within)
            size = int(size_line.strip().split(b";")[0], 16)
            body = await asyncio.wait_for(self._reader.readexactly(size + 2), within)
        except (TimeoutError, asyncio.IncompleteReadError):
            return None
        chunk = body[:size]
        self.chunks.append(chunk)
        self.parser.feed(chunk)
        return chunk

    async def read_events(self, count: int, within: float = 1.0) -> list[SSEEvent]:
        """Read until `count` dispatchable events have been parsed."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + within
        while len(self.parser.events) < count:
            remaining = deadline - loop.time()
            if remaining <= 0 or await self.next_chunk(remaining) is None:
                break
        return self.parser.events

    async def drain_quiet(self, quiet_for: float = 0.15) -> None:
        """Keep reading until the server has said nothing for `quiet_for`."""
        while await self.next_chunk(quiet_for) is not None:
            pass

    async def close(self) -> None:
        self._writer.close()
        with contextlib.suppress(Exception):
            await self._writer.wait_closed()


@contextlib.asynccontextmanager
async def serving(cfg):
    """The real app, real lifespan, real uvicorn, ephemeral port."""
    app = create_app(cfg)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    # pytest owns this process's signal handlers; uvicorn must not take them.
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
    task = asyncio.create_task(server.serve())
    try:
        # ruff ASYNC110: uvicorn publishes a `started` flag, not an
        # asyncio.Event, so there is nothing here to await.
        while not server.started:  # noqa: ASYNC110
            await asyncio.sleep(0.005)
        yield server.servers[0].sockets[0].getsockname()[1], app.state.hub
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=5)
        except TimeoutError:  # a stream still open: stop caring about it
            server.force_exit = True
            await task


@pytest.fixture
def live(config):
    """`async with live() as (port, hub)` — the app, actually listening."""

    def _make(**kw):
        # Long keepalive by default: comment frames are correct behaviour but
        # they are noise in every test that is not about them.
        kw.setdefault("sse_keepalive_s", 30.0)
        return serving(config(**kw))

    return _make


def log(seq: int, run_id: str = "r1", **kw):
    return make_message("log", run_id=run_id, seq=seq, message=f"line {seq}", **kw)


async def publish(hub, *messages, run_id: str = "r1") -> None:
    for msg in messages:
        await hub.broadcaster.publish(run_id, msg)


async def open_stream(port, run_id="r1", last_event_id: str | None = None) -> WireConnection:
    """Open the stream and get past the opening `retry:` frame.

    Returning only once a byte has arrived matters: it is the proof that the
    endpoint has run and its subscription exists, so anything published after
    this call cannot be lost to a race with subscription setup.
    """
    headers = {} if last_event_id is None else {"Last-Event-ID": last_event_id}
    conn = await WireConnection.open(port, f"/api/runs/{run_id}/stream", headers)
    first = await conn.next_chunk()
    assert first == b"retry: 2000\n\n", f"expected the retry frame first, got {first!r}"
    return conn


# ---------------------------------------------------------------------------
# 1. The wire format is what an EventSource requires
# ---------------------------------------------------------------------------


async def test_the_response_announces_itself_as_an_unbuffered_event_stream(live):
    async with live() as (port, _hub):
        conn = await WireConnection.open(port, "/api/runs/r1/stream")
        try:
            assert conn.status == 200
            assert conn.headers["content-type"].startswith("text/event-stream")
            assert conn.headers["cache-control"].startswith("no-cache")
            # Two independent defences against a proxy sitting on the stream
            # until it is "big enough" — see /spike-sse.
            assert "no-transform" in conn.headers["cache-control"]
            assert conn.headers["x-accel-buffering"] == "no"
            # No content-length: a stream that announced its length would be
            # a stream the server had already finished writing.
            assert "content-length" not in conn.headers
        finally:
            await conn.close()


async def test_every_frame_is_framed_the_way_the_sse_grammar_requires(live):
    """id, event and data on every frame, in that order, blank-line delimited.

    Asserted on the exact bytes of the chunk rather than on `in body`: a
    stream that had lost its blank lines would satisfy every substring check
    and render nothing at all.
    """
    async with live() as (port, hub):
        conn = await open_stream(port)
        try:
            await publish(hub, log(0))
            chunk = await conn.next_chunk()
            assert chunk is not None
            text = chunk.decode()
            head, _, tail = text.partition("\n")
            middle, _, rest = tail.partition("\n")
            assert head == "id: 0"
            assert middle == "event: log"
            assert rest.startswith("data: {")
            assert text.endswith("\n\n"), "a frame that does not end blank never dispatches"
            assert "\r" not in text
            # Exactly one blank line, at the end — an empty line in the middle
            # would split this into two frames, the second of them garbage.
            assert text.count("\n\n") == 1
            assert json.loads(rest[len("data: ") : -2])["message"] == "line 0"
        finally:
            await conn.close()


async def test_a_log_line_containing_newlines_cannot_break_the_frame(live):
    """The one framing bug a real payload can cause on its own.

    A raw newline inside `data:` ends the data line; a raw blank line ends the
    *event*. A model that logs a stack trace, or a solver that logs a line
    beginning "data:", would otherwise inject frames into the stream.
    """
    nasty = 'traceback:\nline two\n\nid: 999\nevent: status\ndata: {"injected": true}\n'
    async with live() as (port, hub):
        conn = await open_stream(port)
        try:
            await publish(hub, make_message("log", run_id="r1", seq=7, message=nasty))
            events = await conn.read_events(1)
            assert len(events) == 1, "the payload split itself into extra frames"
            assert events[0].id == "7" and events[0].type == "log"
            assert json.loads(events[0].data)["message"] == nasty
        finally:
            await conn.close()


async def test_a_multibyte_character_survives_the_chunk_boundary(live):
    async with live() as (port, hub):
        conn = await open_stream(port)
        try:
            await publish(hub, make_message("log", run_id="r1", seq=0, message="µ→∞ 🙂"))
            events = await conn.read_events(1)
            assert json.loads(events[0].data)["message"] == "µ→∞ 🙂"
        finally:
            await conn.close()


async def test_an_idle_stream_sends_comment_frames_and_nothing_else(live):
    """Keepalives must be comments, not events: a browser must not see them.

    They also must not disturb `Last-Event-ID` — a comment frame carrying an
    id would move the client's cursor past messages it never received.
    """
    async with live(sse_keepalive_s=0.05) as (port, _hub):
        conn = await open_stream(port, run_id="idle-run")
        try:
            await asyncio.sleep(0)
            for _ in range(3):
                chunk = await conn.next_chunk(0.5)
                assert chunk == b": keepalive\n\n"
            assert conn.parser.comments == ["keepalive"] * 3
            assert conn.parser.events == []
            assert conn.parser.last_event_id == ""
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# 2. event: carries the message type, on every frame, for all four types
# ---------------------------------------------------------------------------


async def test_all_four_message_types_arrive_as_named_events(live):
    """The whole point of ADR-001 action item 3.

    A frame with no `event:` defaults to the name "message" in the browser,
    which fires no `addEventListener('log'|'progress'|'status'|'result')` at
    all — the client would go silently blank rather than error.
    """
    async with live() as (port, hub):
        conn = await open_stream(port)
        try:
            await publish(
                hub,
                log(0),
                make_message("progress", run_id="r1", seq=1, elapsed_seconds=1.5),
                make_message("status", run_id="r1", seq=2, status="RUNNING"),
                make_message("result", run_id="r1", seq=3, row_count=4),
            )
            events = await conn.read_events(4)
            assert conn.parser.types == ["log", "progress", "status", "result"]
            assert conn.parser.ids == ["0", "1", "2", "3"]
            # The name is not a substitute for the body: the type is in both,
            # so a client that only listens to `message` still works.
            assert [json.loads(e.data)["type"] for e in events] == conn.parser.types
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# 3. Last-Event-ID resume, and exactly where its boundary falls
# ---------------------------------------------------------------------------


async def test_a_reconnect_resumes_after_the_last_id_the_browser_saw(live):
    async with live() as (port, hub):
        first = await open_stream(port)
        await publish(hub, log(0), log(1), log(2), log(3))
        await first.read_events(4)
        assert first.parser.ids == ["0", "1", "2", "3"]
        # The value a browser would send back, computed by the parser from the
        # stream itself rather than assumed.
        resume_from = first.parser.last_event_id
        assert resume_from == "3"
        await first.close()

        second = await open_stream(port, last_event_id=resume_from)
        try:
            # Straddle the boundary: below it, on it, and above it.
            await publish(hub, log(2), log(3), log(4), log(5))
            await second.read_events(2)
            await second.drain_quiet()
            assert second.parser.ids == ["4", "5"], (
                "off-by-one on the resume boundary: either a duplicate or a "
                "permanently missing message"
            )
        finally:
            await second.close()


async def test_a_reconnect_does_not_replay_the_snapshot(live):
    """Resume means resume. A snapshot on top of a resumed stream would hand
    the client a status it already has, with an id it has already passed."""
    async with live() as (port, hub):
        await publish(
            hub,
            make_message("status", run_id="r1", seq=0, status="RUNNING"),
            make_message("progress", run_id="r1", seq=1, elapsed_seconds=2.0),
        )
        conn = await open_stream(port, last_event_id="1")
        try:
            await conn.drain_quiet()
            assert conn.parser.events == []
        finally:
            await conn.close()


async def test_a_gap_that_opened_while_disconnected_is_not_recovered_by_resume(live):
    """The limit of the native mechanism, pinned deliberately.

    `Last-Event-ID` resumes the *live tail*; this app keeps no replay buffer
    (RunSnapshot is two messages, not history). Anything published while
    nobody was attached is gone from the live path and only the backfill
    endpoint can produce it. A client that treats a reconnect as "I am caught
    up now" will silently miss a terminal status that landed during the gap —
    it must reconcile the seq range itself.
    """
    async with live() as (port, hub):
        first = await open_stream(port)
        await publish(hub, log(0))
        await first.read_events(1)
        await first.close()

        # Missed entirely: nobody is subscribed.
        await publish(hub, log(1), make_message("status", run_id="r1", seq=2, status="SUCCEEDED"))

        second = await open_stream(port, last_event_id="0")
        try:
            await publish(hub, log(3))
            await second.read_events(1)
            await second.drain_quiet()
            assert second.parser.ids == ["3"]  # not 1, not 2 — those need backfill
        finally:
            await second.close()


# ---------------------------------------------------------------------------
# 4. A fresh connection gets a snapshot, and a snapshot is not a replay
# ---------------------------------------------------------------------------


async def test_a_fresh_viewer_gets_a_snapshot_rather_than_the_whole_run(live):
    async with live() as (port, hub):
        await publish(
            hub,
            make_message("status", run_id="r1", seq=0, status="RUNNING"),
            make_message("progress", run_id="r1", seq=1, elapsed_seconds=1.0),
            log(2),
            log(3),
            make_message("progress", run_id="r1", seq=4, elapsed_seconds=9.0),
        )
        conn = await open_stream(port)
        try:
            await conn.read_events(2)
            await conn.drain_quiet()
            assert conn.parser.types == ["status", "progress"]
            # The *latest* progress point, not the first, and no logs at all:
            # this is current state, not history.
            assert conn.parser.ids == ["0", "4"]
            assert conn.parser.payloads()[1]["elapsed_seconds"] == 9.0
        finally:
            await conn.close()


async def test_a_run_with_nothing_published_yet_streams_no_snapshot_at_all(live):
    async with live() as (port, _hub):
        conn = await open_stream(port, run_id="never-seen")
        try:
            await conn.drain_quiet()
            assert conn.parser.events == []
        finally:
            await conn.close()


async def test_the_snapshot_leaves_the_browsers_cursor_at_the_highest_seq_it_saw(live):
    """A terminal run is the case that exposes this.

    The snapshot is emitted status-then-progress, but at the end of a run the
    terminal status has the *higher* seq — so emitting it first would leave
    `Last-Event-ID` on the lower progress id, and a subsequent reconnect would
    ask to resume from a point the client is already past. The client would
    survive (it has the status; it dedupes by seq), but its cursor would be
    wrong, and everything downstream — gap detection, backfill `after_seq` —
    is computed from that cursor.
    """
    async with live() as (port, hub):
        await publish(
            hub,
            make_message("progress", run_id="r1", seq=8, elapsed_seconds=30.0),
            make_message("status", run_id="r1", seq=9, status="SUCCEEDED"),
        )
        conn = await open_stream(port)
        try:
            await conn.read_events(2)
            await conn.drain_quiet()
            assert sorted(conn.parser.ids) == ["8", "9"]
            assert conn.parser.last_event_id == "9", (
                "the last id on the wire is not the highest seq in the snapshot; "
                "a reconnect would resume from behind where the client actually is"
            )
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# 5. A malformed Last-Event-ID degrades to a fresh connection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param("", id="empty"),
        pytest.param("   ", id="whitespace"),
        pytest.param("not-a-number", id="non_numeric"),
        pytest.param("4.5", id="float"),
        pytest.param("12abc", id="trailing_junk"),
        pytest.param("-1", id="negative"),
        pytest.param("9" * 40, id="enormous"),
        pytest.param("NaN", id="nan"),
    ],
)
async def test_a_nonsense_last_event_id_degrades_to_a_fresh_connection(live, bad):
    """None of these can come from a browser that received our ids — they come
    from a stale cache, a rewriting proxy, or a hand-rolled client. Every one
    of them must land on the fresh-connection path: no error, and above all no
    silently empty stream, which is the failure that looks like "the app is
    just slow" for as long as anyone is willing to wait.
    """
    async with live() as (port, hub):
        await publish(hub, make_message("status", run_id="r1", seq=3, status="RUNNING"))
        conn = await open_stream(port, last_event_id=bad)
        try:
            await conn.read_events(1)
            assert conn.parser.types == ["status"], f"Last-Event-ID {bad!r} swallowed the snapshot"
            await publish(hub, log(4))
            await conn.read_events(2)
            assert conn.parser.ids == ["3", "4"], f"Last-Event-ID {bad!r} swallowed live messages"
        finally:
            await conn.close()


async def test_a_last_event_id_the_run_has_passed_still_streams_what_follows(live):
    """The legitimate ceiling case: a valid id, just older than everything."""
    async with live() as (port, hub):
        conn = await open_stream(port, last_event_id="0")
        try:
            await publish(hub, log(1))
            await conn.read_events(1)
            assert conn.parser.ids == ["1"]
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# 6. client_visible=False means the seq stream a client sees has gaps
# ---------------------------------------------------------------------------


async def test_the_seq_stream_a_browser_sees_legitimately_has_gaps(live):
    """Gap detection on the client must not treat a gap as an error.

    `seq` is consumed by every message the job creates, including the ones it
    never sends live: `client_visible=False` filters the *live send* only
    (job/bus.py::_live_visible), and the durable write already happened. So
    a browser watching a Gurobi run sees 0, 1, 3, 4 with no 2, and that is
    correct rather than a dropped message.

    These gaps can never be closed. `app/server/repository.py::messages_since` — the
    backfill query — carries `AND client_visible` on its log branch, so seq 2
    is not in the live stream *and* not in the backfill. A client that retries
    backfill until its seq range is contiguous will retry forever.
    """
    # The enforcement point, imported rather than restated. It lives in the
    # OTHER deployable unit, though, and `job/` carries its own copy of the
    # envelope — so an enum member from `job.shared` is not the same object as
    # its twin from `shared`, and `_live_visible`'s `msg.type is
    # MessageType.LOG` is False for a message this file built. Byte-identical
    # source, distinct types.
    #
    # The two copies never meet in a real process: the job emits bytes and the
    # app parses them. They meet here, so this hands the rule a message typed
    # the way the job types its own, and publishes the app-typed one.
    from job.bus import _live_visible
    from job.shared.envelope import MessageAdapter as JobMessage

    def as_the_job_sees_it(msg):
        return JobMessage.validate_python(msg.model_dump())

    emitted = [
        log(0),
        log(1),
        log(2, client_visible=False, level="DEBUG"),  # raw solver chatter
        log(3),
        make_message("status", run_id="r1", seq=4, status="SUCCEEDED"),
    ]
    async with live() as (port, hub):
        conn = await open_stream(port)
        try:
            await publish(hub, *[m for m in emitted if _live_visible(as_the_job_sees_it(m))])
            await conn.read_events(4)
            await conn.drain_quiet()
            assert conn.parser.ids == ["0", "1", "3", "4"]
            # And the cursor is the highest seq seen, not a count of messages.
            assert conn.parser.last_event_id == "4"
        finally:
            await conn.close()


async def test_backfill_cannot_close_a_client_visible_gap(app_and_hub):
    """The other half of the claim above, against the real query text."""
    _app, hub = app_and_hub()
    sql = ScriptedSql()
    repo = RunRepository(sql, hub.tables)
    await repo.messages_since("r1", after_seq=0, limit=100)
    (text, params), *_ = sql.queries
    log_branch = text.split("UNION ALL")[0]
    assert "client_visible" in log_branch.split("WHERE")[1]
    # And the cursor is bound as an INT, not a string: "2" > "12" is the
    # comparison that stalls a backfill cursor at seq 9.
    assert next(p for p in params if p.name == "after_seq").type == "INT"


# ---------------------------------------------------------------------------
# The bytes, on the record
# ---------------------------------------------------------------------------


@dataclass
class _Recorded:
    frames: list[bytes] = field(default_factory=list)


async def test_the_representative_frames_are_recorded_verbatim(live, capsys):
    """Prints the exact bytes with -s. Assertions here are the contract; the
    print is so a human can see what an EventSource is actually being fed."""
    recorded = _Recorded()
    async with live() as (port, hub):
        conn = await open_stream(port)
        recorded.frames.append(b"retry: 2000\n\n")
        try:
            await publish(
                hub,
                make_message("status", run_id="demo", seq=0, status="RUNNING"),
                make_message(
                    "progress", run_id="demo", seq=1, elapsed_seconds=1.25, percent_complete=40.0
                ),
                make_message("log", run_id="demo", seq=2, message="solving"),
                make_message("result", run_id="demo", seq=3, row_count=2, final=True),
            )
            for _ in range(4):
                chunk = await conn.next_chunk()
                assert chunk is not None
                recorded.frames.append(chunk)
        finally:
            await conn.close()

    for frame in recorded.frames:
        print(repr(frame))
    assert all(f.endswith(b"\n\n") for f in recorded.frames)
    assert all(
        f.startswith(b"id: ") and b"\nevent: " in f and b"\ndata: " in f
        for f in recorded.frames[1:]
    )
