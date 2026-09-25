"""The app's side of the RPC channel: where a job attaches.

One endpoint, one protocol, one direction of surprise removed. v3 had a
WebSocket carrying tagged msgpack control frames *plus* an HTTP push fallback,
and the two had to be kept from diverging by hand — a run observed over one
would otherwise look different from the same run observed over the other. Both
are replaced by this.

The HTTP push route is gone with them, and its absence is deliberate rather
than pending. It existed as a fallback for a WebSocket that might not survive
the Apps ingress; the spikes settled that it does, and a one-way fallback
cannot carry a `cancel` ack or a `replay` response anyway, so keeping it would
mean maintaining a channel over which half the protocol does not work.

**Requests initiated by the APP** — `cancel`, `replay` — are the reason this
file is more than a receive loop. Sending one means holding a future for its
reply, because the job answers on the same socket, interleaved with whatever
telemetry it is streaming. `PendingCalls` is that, and nothing more.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from shared.envelope import MessageAdapter, MessageType
from shared.rpc import (
    PROTOCOL_VERSION,
    ErrorCode,
    Method,
    Request,
    Response,
    RpcError,
    check_hello_version,
    failure,
    parse,
    success,
)

from ..services import ServiceHub

log = logging.getLogger(__name__)
router = APIRouter(tags=["rpc"])

_KNOWN_TYPES = frozenset(m.value for m in MessageType)


# THE PROXY IS THE GATE, and this app authenticates nothing itself.
#
# Databricks Apps sits a proxy in front of this process that lets nothing
# through without a Databricks OAuth token from a principal holding `CAN_USE`
# on the app. It is platform-enforced, and the proof is the shape of its
# refusal: an unauthenticated upgrade gets a **302 to the OAuth login page**,
# never a 401, so nothing unauthenticated has ever reached this function.
#
# There used to be a second check here — `X-DBX-App-Token`, a shared secret
# from the workspace secret scope, carried to the job as a job parameter at
# trigger time. It is gone, and what it cost was out of all proportion to what
# it added on top of the proxy:
#
#   * a secret to create, grant and rotate;
#   * a declared app resource, which is validated at DEPLOY time and 404s the
#     whole deploy when the key is absent;
#   * a `DBX_APP_TOKEN` job parameter, declared on both sides of
#     JOB_PARAMETER_NAMES because Databricks rejects an undeclared one;
#   * and a silent failure mode that pointed the wrong way — an unset token
#     meant `return True`, so a typo opened the ingress instead of closing it.
#
# **The consequence, stated plainly:** anything that can reach this app can
# open a job socket. On Databricks that set is exactly the principals granted
# `CAN_USE`, which is the same set the proxy already trusts. If this app is
# ever run somewhere WITHOUT a proxy in front of it, it is unauthenticated —
# so do not.


#: The methods this app answers, published in `hello`'s result. LSP-style:
#: a key per method, its value that method's options (none yet).
APP_CAPABILITIES: dict[str, dict[str, Any]] = {
    Method.HELLO: {},
    Method.TELEMETRY: {},
    Method.BYE: {},
    Method.PING: {},
}

#: WebSocket close code for a refused `hello`: 1008, "policy violation" —
#: the frames were fine, the job is simply not one this app will observe.
CLOSE_INCOMPATIBLE = 1008


@router.websocket("/ws/job/{run_id}")
async def job_socket(websocket: WebSocket, run_id: str) -> None:
    hub: ServiceHub | None = getattr(websocket.app.state, "hub", None)
    if hub is None:
        await websocket.close(code=1011, reason="services not initialised")
        return
    await websocket.accept()
    # NOT registered yet. A job is reachable for cancel/replay only once its
    # `hello` has passed the version check — otherwise the app could send a
    # command to a job whose protocol it has just refused to speak.
    attached = False

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                frame = parse(raw)
            except RpcError as exc:
                await websocket.send_text(failure(None, exc))
                continue

            if isinstance(frame, Response):
                # A reply to something WE asked — cancel, replay. Hand it to
                # whoever is waiting; an unmatched id is a late reply to a
                # call that already timed out, and dropping it is right.
                hub.job_sockets.resolve(run_id, frame)
                continue

            if frame.method == Method.HELLO:
                if not await _hello(websocket, run_id, frame):
                    return  # refused, answered, and closed
                if not attached:
                    hub.job_sockets.register(run_id, websocket)
                    attached = True
                    log.info("job attached for run %s", run_id)
                continue

            if not attached:
                # The version gate is only a gate if nothing gets past it.
                # A conforming job's first frame is always `hello`.
                if frame.is_notification:
                    log.warning(
                        "dropping %s on run %s: sent before an accepted hello",
                        frame.method,
                        run_id,
                    )
                else:
                    await websocket.send_text(
                        failure(
                            frame.id,
                            RpcError(ErrorCode.INVALID_REQUEST, "send hello before anything else"),
                        )
                    )
                continue

            await _handle(hub, websocket, run_id, frame)
    except WebSocketDisconnect:
        log.info("job detached from run %s", run_id)
    except Exception:  # noqa: BLE001
        log.exception("job socket for %s failed", run_id)
    finally:
        hub.job_sockets.unregister(run_id, websocket)


async def _hello(websocket: WebSocket, run_id: str, req: Request) -> bool:
    """Apply the version rule to a `hello`. True if accepted (and answered).

    A refusal is answered with a JSON-RPC error — carrying the app's version
    and the rule in `data`, so the job can log exactly why — and then the
    socket is closed with a reason naming both versions. The job treats that
    as "run unobserved" and stops retrying: the answer cannot change until
    one side is redeployed.
    """
    try:
        job_version = check_hello_version(req.params)
        capabilities = req.params.get("capabilities", {})
        if not isinstance(capabilities, dict):
            raise RpcError(ErrorCode.INVALID_PARAMS, "capabilities must be an object")
    except RpcError as exc:
        log.warning("refusing job on run %s: %s", run_id, exc.message)
        if not req.is_notification:
            await websocket.send_text(failure(req.id, exc))
        # Close reasons are capped at 123 bytes by the WebSocket spec.
        reason = f"hello refused: {exc.message}".encode()[:123].decode(errors="ignore")
        await websocket.close(code=CLOSE_INCOMPATIBLE, reason=reason)
        return False

    # `next_seq` is the job telling us where it is picking up. A job that has
    # been running unobserved for an hour attaches at seq 4,000, and saying so
    # is what lets a client know it has a gap rather than inferring one from a
    # jump it might read as a bug.
    log.info(
        "job hello for %s at seq %s, protocol %s, capabilities %s",
        run_id,
        req.params.get("next_seq"),
        job_version,
        sorted(capabilities),
    )
    if not req.is_notification:
        await websocket.send_text(
            success(
                req.id,
                {
                    "observed": True,
                    "run_id": run_id,
                    "protocol_version": PROTOCOL_VERSION,
                    "capabilities": APP_CAPABILITIES,
                },
            )
        )
    return True


async def _handle(hub: ServiceHub, websocket: WebSocket, run_id: str, req: Request) -> None:
    try:
        result = await _invoke(hub, run_id, req)
    except RpcError as exc:
        if not req.is_notification:
            await websocket.send_text(failure(req.id, exc))
        return
    except Exception as exc:  # noqa: BLE001
        log.exception("handler for %s raised", req.method)
        if not req.is_notification:
            await websocket.send_text(failure(req.id, RpcError(ErrorCode.INTERNAL_ERROR, str(exc))))
        return

    # A notification gets no reply, ever.
    if not req.is_notification:
        await websocket.send_text(success(req.id, result))


async def _invoke(hub: ServiceHub, run_id: str, req: Request) -> Any:
    # `hello` never reaches here: `job_socket` handles it, because refusing
    # one means closing the socket, which a handler's return value cannot do.

    if req.method == Method.PING:
        return {"pong": True, "run_id": run_id}

    if req.method == Method.BYE:
        log.info("job says the run is over: %s", run_id)
        return None

    if req.method == Method.TELEMETRY:
        messages = req.params.get("messages")
        if not isinstance(messages, list):
            raise RpcError(ErrorCode.INVALID_PARAMS, "messages must be an array")
        # THE PARSE BOUNDARY, and it is per record. A record this app cannot
        # read — a `type` from a newer minor version, or plain garbage — is
        # counted, logged once per batch, and skipped. It never raises out of
        # here: one bad record must not cost the batch its other records, and
        # nothing a job sends may cost the socket loop. Nothing is lost by
        # skipping: the telemetry volume has every record regardless.
        unknown_types: dict[str, int] = {}
        malformed = 0
        for raw in messages:
            kind = raw.get("type") if isinstance(raw, dict) else None
            if isinstance(kind, str) and kind not in _KNOWN_TYPES:
                kind = kind[:64]
                unknown_types[kind] = unknown_types.get(kind, 0) + 1
                continue
            try:
                msg = MessageAdapter.validate_python(raw)
            except Exception:  # noqa: BLE001
                malformed += 1
                continue
            if msg.run_id != run_id:
                # A job on one socket must not be able to write into another
                # run's stream, however it got the id wrong.
                log.warning("job on %s sent a message for %s; ignoring", run_id, msg.run_id)
                continue
            await hub.ingest(run_id, msg)
        if unknown_types:
            log.info(
                "skipped %d record(s) of unknown type on run %s: %s "
                "(a newer job minor version? they are on the telemetry volume)",
                sum(unknown_types.values()),
                run_id,
                dict(sorted(unknown_types.items())),
            )
        if malformed:
            log.warning("skipped %d malformed record(s) on run %s", malformed, run_id)
        return None

    raise RpcError(ErrorCode.METHOD_NOT_FOUND, f"no method {req.method!r}")
