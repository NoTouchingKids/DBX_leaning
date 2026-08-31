"""The job<->app protocol.

Framing tests are cheap and mostly obvious; the ones worth reading are about
the distinctions the protocol makes deliberately — notification vs request,
and refusing to guess at a frame it does not recognise.
"""

from __future__ import annotations

import json

import pytest

from shared.rpc import (
    ErrorCode,
    Method,
    Request,
    Response,
    RpcError,
    failure,
    notification,
    parse,
    request,
    success,
)


def _round(frame: bytes):
    return parse(frame)


def test_a_request_carries_an_id_and_expects_a_reply():
    got = _round(request(Method.CANCEL, {"requested_by": "kp"}, id=7))
    assert isinstance(got, Request)
    assert got.method == Method.CANCEL
    assert got.params == {"requested_by": "kp"}
    assert got.id == 7
    assert got.is_notification is False


def test_a_notification_has_no_id_and_that_is_the_whole_difference():
    """Telemetry is a notification, and it is the bulk of all traffic.

    Acknowledging every batch would double the frames to tell us something the
    durable path already guarantees — so `id` is absent, and a reply to one
    would be a protocol error rather than merely wasteful.
    """
    got = _round(notification(Method.TELEMETRY, {"messages": [{"seq": 1}]}))
    assert isinstance(got, Request)
    assert got.is_notification is True
    assert got.id is None
    assert "id" not in json.loads(notification(Method.TELEMETRY))


def test_a_response_carries_exactly_one_of_result_or_error():
    ok = _round(success(7, {"accepted": True}))
    assert isinstance(ok, Response)
    assert ok.ok and ok.result == {"accepted": True} and ok.error is None

    bad = _round(failure(7, RpcError(ErrorCode.UNKNOWN_RUN, "no such run", data={"run_id": "r9"})))
    assert isinstance(bad, Response)
    assert not bad.ok
    assert bad.error == {
        "code": ErrorCode.UNKNOWN_RUN,
        "message": "no such run",
        "data": {"run_id": "r9"},
    }


def test_an_unrecognised_frame_is_rejected_rather_than_guessed_at():
    """Sniffing frames apart by which keys happen to be present is a decoding
    bug waiting on a schema change — the same reason v3's frames carried an
    explicit tag. A frame with neither `method` nor `result`/`error` is not a
    frame this protocol has, and saying so beats inventing one."""
    with pytest.raises(RpcError) as exc:
        parse(json.dumps({"jsonrpc": "2.0", "id": 1, "payload": {"seq": 3}}))
    assert exc.value.code == ErrorCode.INVALID_REQUEST


def test_the_version_is_checked_so_a_mismatch_is_not_silent():
    with pytest.raises(RpcError) as exc:
        parse(json.dumps({"jsonrpc": "1.0", "method": "ping"}))
    assert exc.value.code == ErrorCode.INVALID_REQUEST


def test_malformed_json_is_a_parse_error_not_a_crash():
    with pytest.raises(RpcError) as exc:
        parse(b"{not json")
    assert exc.value.code == ErrorCode.PARSE_ERROR


def test_positional_params_are_refused():
    """Legal JSON-RPC, deliberately unsupported: one shape means two call
    sites cannot quietly disagree about argument order."""
    with pytest.raises(RpcError) as exc:
        parse(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "cancel", "params": ["kp"]}))
    assert exc.value.code == ErrorCode.INVALID_PARAMS


def test_a_method_must_be_a_non_empty_string():
    for bad in (None, "", 42):
        with pytest.raises(RpcError):
            parse(json.dumps({"jsonrpc": "2.0", "id": 1, "method": bad}))


def test_the_method_set_is_small_and_changing_it_is_a_decision():
    """A guard, not a tautology.

    docs/v4-rewrite-plan.md gates the app's language migration on this surface
    having stopped changing. A method added casually costs that, so this test
    exists to make the addition deliberate — if you are here because it failed,
    update the plan's Slice-1 method list in the same commit.
    """
    declared = {v for k, v in vars(Method).items() if not k.startswith("_") and isinstance(v, str)}
    assert declared == {"telemetry", "hello", "bye", "cancel", "replay", "ping"}


def test_errors_stay_clear_of_the_reserved_jsonrpc_range():
    ours = (ErrorCode.UNKNOWN_RUN, ErrorCode.RECORDS_GONE)
    for code in ours:
        assert not (-32768 <= code <= -32000), f"{code} collides with JSON-RPC's reserved range"
