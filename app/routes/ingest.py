"""Where a job's messages come in: WebSocket first, HTTP push as fallback.

Both land in the same ``hub.ingest`` — the two channels must not diverge, or a
run observed over one path would look different from the same run observed
over the other.

Only the WebSocket carries anything *back* (cancel). HTTP push is one-way by
design, not by omission.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect, status

from shared.envelope import MessageAdapter
from shared.protocol import ControlFrame, ControlKind, pack_frame, pong, unpack_frame

from ..deps import get_hub
from ..services import ServiceHub

log = logging.getLogger(__name__)
router = APIRouter(tags=["ingest"])


def _authorised(hub: ServiceHub, presented: str | None) -> bool:
    """The job's own credential, distinct from user auth.

    Whether a job can even reach this endpoint through the Databricks Apps
    ingress — and with what auth context — is one of the open questions
    /spike-ws exists to answer. This check is what the app controls.
    """
    expected = hub.config.job_token
    if not expected:
        return True  # nothing configured: development posture
    if not presented:
        return False
    scheme, _, token = presented.partition(" ")
    return token.strip() == expected if scheme.lower() == "bearer" else presented == expected


@router.websocket("/ws/job/{run_id}")
async def job_socket(websocket: WebSocket, run_id: str) -> None:
    hub: ServiceHub | None = getattr(websocket.app.state, "hub", None)
    if hub is None:
        await websocket.close(code=1011, reason="services not initialised")
        return
    if not _authorised(hub, websocket.headers.get("authorization")):
        await websocket.close(code=1008, reason="unauthorised")
        return

    await websocket.accept()
    hub.job_sockets.register(run_id, websocket)
    log.info("job attached for run %s", run_id)

    try:
        while True:
            raw = await websocket.receive_bytes()
            try:
                frame = unpack_frame(raw)
            except Exception:  # noqa: BLE001
                log.warning("undecodable frame from job on run %s", run_id)
                continue

            if isinstance(frame, ControlFrame):
                await _handle_control(hub, websocket, run_id, frame)
                continue
            if frame.run_id != run_id:
                log.warning(
                    "job on run %s sent a message for %s; ignoring", run_id, frame.run_id
                )
                continue
            await hub.ingest(run_id, frame)
    except WebSocketDisconnect:
        log.info("job detached from run %s", run_id)
    except Exception:  # noqa: BLE001
        log.exception("job socket for %s failed")
    finally:
        hub.job_sockets.unregister(run_id, websocket)


async def _handle_control(
    hub: ServiceHub, websocket: WebSocket, run_id: str, frame: ControlFrame
) -> None:
    if frame.kind is ControlKind.HELLO:
        log.info("job hello for %s: %s", run_id, frame.payload)
        await websocket.send_bytes(
            pack_frame(
                ControlFrame(
                    kind=ControlKind.HELLO_ACK,
                    run_id=run_id,
                    payload={"observed": True},
                )
            )
        )
    elif frame.kind is ControlKind.PING:
        await websocket.send_bytes(pack_frame(pong(run_id)))
    elif frame.kind is ControlKind.BYE:
        log.info("job says the run is over: %s", run_id)


@router.post("/api/runs/{run_id}/push", status_code=status.HTTP_202_ACCEPTED)
async def http_push(run_id: str, request: Request) -> dict:
    """One-way fallback ingest. Cannot carry a reply, and does not pretend to."""
    hub = get_hub(request)
    if not _authorised(hub, request.headers.get("authorization")):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unauthorised")

    body = await request.json()
    raw_messages = body.get("messages") if isinstance(body, dict) else None
    if not isinstance(raw_messages, list):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "expected {'messages': [...]}")

    accepted = 0
    for raw in raw_messages:
        try:
            msg = MessageAdapter.validate_python(raw)
        except Exception:  # noqa: BLE001
            log.warning("dropping malformed pushed message for %s", run_id)
            continue
        if msg.run_id != run_id:
            continue
        await hub.ingest(run_id, msg)
        accepted += 1
    return {"accepted": accepted, "received": len(raw_messages)}
