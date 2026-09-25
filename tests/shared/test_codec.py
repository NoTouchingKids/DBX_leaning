"""One codec, and the property that used to need two.

v3 had msgpack job→app and JSON to the browser, and this file existed largely
to assert the two carried identical logical content — because a message that
means one thing over the socket and another in storage is how v3's real bugs
happened, surfacing only after a reconnect, the one path that exercises both.

v4 has only JSON, so that particular drift cannot occur. What is still worth
pinning is the boundary the two-codec rule was protecting: `to_jsonable`
produces plain data, and nothing that encodes it needs to know what a
`Message` is. That is what makes swapping the encoding a delivery decision
rather than a protocol change — and what would let a future non-Python reader
parse a telemetry part file with nothing but a JSON library.
"""

from __future__ import annotations

import json

import pytest

from shared.codec import decode_json, encode_json, to_jsonable
from shared.envelope import make_message


def sample_messages():
    return [
        make_message("log", run_id="r1", seq=0, ts=1, message="hello"),
        make_message("log", run_id="r1", seq=1, ts=2, message="quiet", client_visible=False),
        make_message(
            "progress",
            run_id="r1",
            seq=2,
            ts=3,
            elapsed_seconds=1.5,
            percent_complete=42.0,
            primary_metric=0.25,
            primary_metric_label="mip_gap",
            payload={"nodes": 17, "nested": {"a": [1, 2]}},
        ),
        make_message("status", run_id="r1", seq=3, ts=4, status="RUNNING", terminal=False),
        make_message(
            "result",
            run_id="r1",
            seq=4,
            ts=5,
            row_count=3,
            preview=[{"x": 1, "y": 2.5}],
            fetch_hint={"table": "main.dbx_leaning.results_x"},
        ),
    ]


@pytest.mark.parametrize("msg", sample_messages(), ids=lambda m: m.type.value)
def test_a_message_round_trips_unchanged(msg):
    assert decode_json(encode_json(msg)) == msg


@pytest.mark.parametrize("msg", sample_messages(), ids=lambda m: m.type.value)
def test_the_encoded_form_is_plain_data_with_no_pydantic_left_in_it(msg):
    """The boundary that matters: whatever encodes a message must not need to
    know what a `Message` is.

    This is what lets the telemetry part files be readable by anything with a
    JSON parser — the ingestion job, an operator, a future harness in another
    language — and what makes changing the encoding a delivery decision rather
    than a protocol change.
    """
    raw = to_jsonable(msg)
    assert isinstance(raw, dict)
    # json.dumps is the check: it fails on anything that is not plain data.
    round_tripped = json.loads(json.dumps(raw))
    assert round_tripped == raw


def test_enums_encode_as_their_string_values():
    """A reader in another language gets `"INFO"`, not an object."""
    msg = make_message("log", run_id="r1", seq=0, ts=1, message="x", level="WARNING")
    raw = to_jsonable(msg)
    assert raw["level"] == "WARNING"
    assert raw["type"] == "log"


def test_an_open_status_survives_encoding():
    """A model-defined status is a plain string all the way through — there is
    no enum to reject it on the way out or coerce it on the way in."""
    msg = make_message("status", run_id="r1", seq=0, ts=1, status="CALIBRATING", terminal=False)
    assert json.loads(encode_json(msg))["status"] == "CALIBRATING"
    assert decode_json(encode_json(msg)).status == "CALIBRATING"


def test_a_batch_keeps_its_order():
    """Telemetry travels in batches, and `seq` is only useful if the order
    survives the trip."""
    msgs = sample_messages()
    encoded = json.dumps([to_jsonable(m) for m in msgs])
    assert [m["seq"] for m in json.loads(encoded)] == [0, 1, 2, 3, 4]
