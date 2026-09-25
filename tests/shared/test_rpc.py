"""The job<->app protocol.

Framing tests are cheap and mostly obvious; the ones worth reading are about
the distinctions the protocol makes deliberately — notification vs request,
and refusing to guess at a frame it does not recognise.
"""

from __future__ import annotations

import json

import pytest

from shared.rpc import (
    PROTOCOL_VERSION,
    ErrorCode,
    Method,
    Request,
    Response,
    RpcError,
    check_hello_version,
    failure,
    is_compatible,
    notification,
    parse,
    parse_protocol_version,
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
    ours = (ErrorCode.UNKNOWN_RUN, ErrorCode.RECORDS_GONE, ErrorCode.INCOMPATIBLE_VERSION)
    for code in ours:
        assert not (-32768 <= code <= -32000), f"{code} collides with JSON-RPC's reserved range"


# --- the protocol version and its compatibility rule -----------------------


@pytest.mark.parametrize(
    "job,app,ok",
    [
        ("1.0", "1.0", True),
        ("1.0", "1.3", True),  # an app upgraded ahead of its jobs keeps observing them
        ("1.3", "1.3", True),
        ("1.4", "1.3", False),  # a job ahead of its app runs unobserved
        ("2.0", "1.9", False),
        ("0.9", "1.0", False),
        ("1.10", "1.9", False),  # numeric, not lexical: 10 > 9
        ("1.9", "1.10", True),
    ],
)
def test_the_rule_is_same_major_and_minor_at_most_the_apps(job, app, ok):
    assert is_compatible(job, app) is ok


@pytest.mark.parametrize(
    "bad", ["1", "1.0.0", "v1.0", "01.0", "1.a", "", " 1.0", 1.0, None, [1, 0]]
)
def test_a_malformed_version_is_refused_not_guessed_at(bad):
    with pytest.raises(ValueError):
        parse_protocol_version(bad)


def test_the_constant_satisfies_its_own_rule():
    assert is_compatible(PROTOCOL_VERSION, PROTOCOL_VERSION)


def test_hello_without_a_version_is_invalid_params():
    with pytest.raises(RpcError) as exc:
        check_hello_version({"run_id": "r", "next_seq": 0})
    assert exc.value.code == ErrorCode.INVALID_PARAMS
    assert exc.value.data["app_protocol_version"] == PROTOCOL_VERSION


def test_hello_with_an_incompatible_version_says_both_versions():
    with pytest.raises(RpcError) as exc:
        check_hello_version({"protocol_version": "9.0"}, app_version="1.0")
    assert exc.value.code == ErrorCode.INCOMPATIBLE_VERSION
    assert exc.value.data["job_protocol_version"] == "9.0"
    assert exc.value.data["app_protocol_version"] == "1.0"
    assert "rule" in exc.value.data


def test_hello_with_a_compatible_version_returns_it():
    assert check_hello_version({"protocol_version": "1.0"}, app_version="1.2") == "1.0"


# --- JSON-RPC 2.0, exactly --------------------------------------------------


def test_every_frame_carries_jsonrpc_2_0_and_is_text():
    for frame in (
        request(Method.HELLO, {}, id=1),
        notification(Method.BYE),
        success(1, None),
        failure(1, RpcError(ErrorCode.INTERNAL_ERROR, "x")),
    ):
        assert isinstance(frame, str), "frames are TEXT, never bytes"
        assert json.loads(frame)["jsonrpc"] == "2.0"


def test_a_response_with_both_result_and_error_is_refused():
    frame = {"jsonrpc": "2.0", "id": 1, "result": {}, "error": {"code": 1, "message": "x"}}
    with pytest.raises(RpcError) as exc:
        parse(json.dumps(frame))
    assert exc.value.code == ErrorCode.INVALID_REQUEST


@pytest.mark.parametrize(
    "error", ["boom", {"message": "no code"}, {"code": "1", "message": "x"}, {"code": 1}]
)
def test_an_error_object_must_have_an_integer_code_and_a_message(error):
    with pytest.raises(RpcError):
        parse(json.dumps({"jsonrpc": "2.0", "id": 1, "error": error}))


@pytest.mark.parametrize("bad_id", [1.5, True, {"a": 1}, [1]])
def test_an_id_is_a_string_or_an_integer(bad_id):
    with pytest.raises(RpcError):
        parse(json.dumps({"jsonrpc": "2.0", "id": bad_id, "method": "ping"}))


def test_a_null_result_is_still_a_success():
    got = parse(success(3, None))
    assert isinstance(got, Response) and got.ok and got.result is None
