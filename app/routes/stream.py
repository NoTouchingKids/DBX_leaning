"""SSE to the browser. One-way, by design.

``id:`` on every event is the message's ``seq``, which is what makes
``EventSource``'s built-in ``Last-Event-ID`` reconnect work with no custom
handshake — the browser sends the last id it saw and this endpoint resumes
after it. Do not hand-roll a ``from_seq`` opening message; the native
mechanism already exists and works.

Two things this endpoint deliberately does *not* do:

- **Backfill from Unity Catalog.** That is a separate, explicit endpoint the
  client calls on demand. Reconnects are usually a gap of milliseconds; going
  to the warehouse on every one of them would be the cost mistake this
  rewrite exists to avoid, dressed up as a feature.
- **Poll anything.** Ever.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from shared.codec import to_jsonable
from shared.envelope import Message

from ..deps import Hub

log = logging.getLogger(__name__)
router = APIRouter(tags=["stream"])

SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    # Defeats response buffering on proxies that honour it. Whether the
    # Databricks Apps ingress does is exactly what /spike-sse measures.
    "X-Accel-Buffering": "no",
}


def _event(msg: Message) -> str:
    return (
        f"id: {msg.seq}\n"
        f"event: {msg.type.value}\n"
        f"data: {json.dumps(to_jsonable(msg), separators=(',', ':'))}\n\n"
    )


def _parse_last_event_id(raw: str | None) -> int | None:
    if raw is None or not raw.strip():
        return None
    try:
        return int(raw)
    except ValueError:
        log.info("ignoring unparseable Last-Event-ID %r", raw[:40])
        return None


@router.get("/api/runs/{run_id}/stream")
async def stream_run(run_id: str, request: Request, hub: Hub) -> StreamingResponse:
    after_seq = _parse_last_event_id(request.headers.get("last-event-id"))
    keepalive = hub.config.sse_keepalive_s
    sub = hub.broadcaster.subscribe(run_id, after_seq=after_seq)

    async def generate() -> AsyncIterator[str]:
        try:
            # Tell EventSource how fast to come back. If the ingress cuts
            # long streams (see /spike-sse), this is what makes the gap short.
            yield "retry: 2000\n\n"

            if after_seq is None:
                # Fresh viewer: whatever the current state is, so the page is
                # not blank until the next live push. Not a replay.
                snapshot = hub.broadcaster.snapshot(run_id)
                if snapshot is not None:
                    for msg in snapshot.messages():
                        yield _event(msg)

            while True:
                if await request.is_disconnected():
                    return
                try:
                    msg = await asyncio.wait_for(sub.queue.get(), timeout=keepalive)
                except (TimeoutError, asyncio.TimeoutError):
                    # Comment-only line: keeps the connection warm and lets an
                    # idle timeout be told apart from a duration cap.
                    yield ": keepalive\n\n"
                    continue

                if msg is None:
                    return
                if after_seq is not None and msg.seq <= after_seq:
                    continue  # already seen before the reconnect
                yield _event(msg)
        finally:
            sub.close()

    return StreamingResponse(generate(), media_type="text/event-stream", headers=SSE_HEADERS)
