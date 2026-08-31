"""SSE behaviour, including the reconnect path EventSource gives us free.

These drive the endpoint directly rather than through httpx's ASGITransport,
which buffers a whole response and so cannot observe a stream at all.
"""

from __future__ import annotations

import asyncio
import json

from starlette.requests import Request

from server.routes.stream import stream_run
from shared.envelope import make_message


def make_request(app, path: str, headers: dict[str, str] | None = None) -> Request:
    async def receive():
        await asyncio.sleep(3600)  # a client that stays connected

    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "root_path": "",
            "query_string": b"",
            "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
            "client": ("test", 1),
            "server": ("test", 80),
            "app": app,
        },
        receive,
    )


def parse_events(text: str) -> list[dict]:
    events, current = [], {}
    for line in text.splitlines():
        if not line:
            if current:
                events.append(current)
                current = {}
            continue
        if line.startswith(":"):
            events.append({"comment": line[1:].strip()})
            continue
        key, _, value = line.partition(":")
        current[key.strip()] = value.strip()
    if current:
        events.append(current)
    return events


async def collect(app, hub, run_id, publish, *, headers=None, deadline=0.4):
    request = make_request(app, f"/api/runs/{run_id}/stream", headers)
    response = await stream_run(run_id, request, hub)

    assert response.media_type == "text/event-stream"
    assert response.headers["cache-control"].startswith("no-cache")
    assert response.headers["x-accel-buffering"] == "no"

    chunks: list[str] = []
    publisher = asyncio.create_task(publish(hub))
    try:
        async with asyncio.timeout(deadline):
            async for chunk in response.body_iterator:
                chunks.append(chunk if isinstance(chunk, str) else chunk.decode())
    except TimeoutError:
        pass
    finally:
        publisher.cancel()
        await asyncio.gather(publisher, return_exceptions=True)
        await response.body_iterator.aclose()
    return parse_events("".join(chunks))


def log(seq: int, run_id: str = "r1"):
    return make_message("log", run_id=run_id, seq=seq, message=f"line {seq}")


async def test_every_event_carries_its_seq_as_the_sse_id(app_and_hub):
    app, hub = app_and_hub()

    async def publish(hub):
        await asyncio.sleep(0.02)
        for seq in (0, 1, 2):
            await hub.broadcaster.publish("r1", log(seq))

    events = [e for e in await collect(app, hub, "r1", publish) if "id" in e]
    assert [e["id"] for e in events] == ["0", "1", "2"]
    assert [e["event"] for e in events] == ["log"] * 3
    assert json.loads(events[0]["data"])["message"] == "line 0"


async def test_last_event_id_resumes_rather_than_replaying(app_and_hub):
    """A reconnecting client gets only what it missed — no custom handshake,
    just the header EventSource sends by itself."""
    app, hub = app_and_hub()

    async def publish(hub):
        await asyncio.sleep(0.02)
        for seq in range(6):
            await hub.broadcaster.publish("r1", log(seq))

    events = await collect(app, hub, "r1", publish, headers={"Last-Event-ID": "3"})
    assert [e["id"] for e in events if "id" in e] == ["4", "5"]


async def test_an_unparseable_last_event_id_is_ignored_not_fatal(app_and_hub):
    app, hub = app_and_hub()

    async def publish(hub):
        await asyncio.sleep(0.02)
        await hub.broadcaster.publish("r1", log(0))

    events = await collect(app, hub, "r1", publish, headers={"Last-Event-ID": "garbage"})
    assert [e["id"] for e in events if "id" in e] == ["0"]


async def test_a_fresh_viewer_gets_the_current_snapshot_not_a_blank_page(app_and_hub):
    app, hub = app_and_hub()
    await hub.broadcaster.publish(
        "r1", make_message("status", run_id="r1", seq=0, status="RUNNING")
    )
    await hub.broadcaster.publish(
        "r1", make_message("progress", run_id="r1", seq=1, elapsed_seconds=9.0)
    )
    await hub.broadcaster.publish("r1", log(2))

    async def publish(hub):
        await asyncio.sleep(10)

    events = [e for e in await collect(app, hub, "r1", publish, deadline=0.2) if "id" in e]
    assert [e["event"] for e in events] == ["status", "progress"]


async def test_a_reconnecting_client_does_not_get_the_snapshot_again(app_and_hub):
    app, hub = app_and_hub()
    await hub.broadcaster.publish(
        "r1", make_message("status", run_id="r1", seq=0, status="RUNNING")
    )

    async def publish(hub):
        await asyncio.sleep(10)

    events = await collect(app, hub, "r1", publish, headers={"Last-Event-ID": "0"}, deadline=0.2)
    assert [e for e in events if "id" in e] == []


async def test_keepalive_comments_are_sent_while_idle(app_and_hub, config):
    # Lets an idle timeout be told apart from a duration cap if the ingress
    # cuts us — see /spike-sse.
    app, hub = app_and_hub(config(sse_keepalive_s=0.03))

    async def publish(hub):
        await asyncio.sleep(10)

    events = await collect(app, hub, "r1", publish, deadline=0.2)
    assert sum(1 for e in events if e.get("comment") == "keepalive") >= 2


async def test_the_stream_tells_the_browser_how_fast_to_reconnect(app_and_hub):
    app, hub = app_and_hub()

    async def publish(hub):
        await asyncio.sleep(10)

    events = await collect(app, hub, "r1", publish, deadline=0.1)
    assert any(e.get("retry") == "2000" for e in events)


async def test_only_this_runs_messages_reach_this_stream(app_and_hub):
    app, hub = app_and_hub()

    async def publish(hub):
        await asyncio.sleep(0.02)
        await hub.broadcaster.publish("r2", log(0, run_id="r2"))
        await hub.broadcaster.publish("r1", log(1))

    events = [e for e in await collect(app, hub, "r1", publish, deadline=0.2) if "id" in e]
    assert [e["id"] for e in events] == ["1"]


async def test_a_slow_subscriber_cannot_grow_memory_without_limit(app_and_hub, config):
    app, hub = app_and_hub(config(sse_queue_max=5))
    sub = hub.broadcaster.subscribe("r1")
    for seq in range(50):
        await hub.broadcaster.publish("r1", log(seq))
    assert sub.queue.qsize() <= 5 and sub.dropped > 0
    sub.close()


async def test_status_evicts_a_log_rather_than_being_dropped(app_and_hub, config):
    app, hub = app_and_hub(config(sse_queue_max=2))
    sub = hub.broadcaster.subscribe("r1")
    for seq in range(2):
        await hub.broadcaster.publish("r1", log(seq))
    await hub.broadcaster.publish(
        "r1", make_message("status", run_id="r1", seq=2, status="SUCCEEDED")
    )

    drained = [sub.queue.get_nowait() for _ in range(sub.queue.qsize())]
    assert any(m.type.value == "status" for m in drained)
    sub.close()
