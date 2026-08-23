"""msgpack and JSON must produce identical logical content.

That interchangeability is the test that the boundary between "protocol" and
"serialisation" sits in the right place — see docs/message-envelope-spec.md.
"""

import pytest

from shared.codec import (
    decode_json,
    decode_msgpack,
    decode_msgpack_many,
    encode_json,
    encode_msgpack,
    encode_msgpack_many,
    to_jsonable,
)
from shared.envelope import make_message


def sample_messages():
    return [
        make_message("log", run_id="r1", seq=0, message="solving", phase="solve", source="gurobi"),
        make_message(
            "log", run_id="r1", seq=1, message="raw chatter", client_visible=False, level="DEBUG"
        ),
        make_message(
            "progress",
            run_id="r1",
            seq=2,
            elapsed_seconds=12.5,
            percent_complete=40.0,
            primary_metric=0.031,
            primary_metric_label="mip_gap",
            payload={"best_bound": 41.2, "incumbent": None, "nodes_explored": 900},
        ),
        make_message("status", run_id="r1", seq=3, status="RUNNING", detail="attached"),
        make_message(
            "result",
            run_id="r1",
            seq=4,
            row_count=8760,
            preview=[{"t": 1, "v": 2.5}],
            fetch_hint={"table": "main.dbx_leaning.results_x", "key": "run_id"},
            chunk_index=2,
            final=False,
        ),
    ]


@pytest.mark.parametrize("msg", sample_messages(), ids=lambda m: f"{m.type.value}-{m.seq}")
def test_both_codecs_round_trip_to_the_same_object(msg):
    assert decode_json(encode_json(msg)) == msg
    assert decode_msgpack(encode_msgpack(msg)) == msg
    # ...and agree with each other, which is the part that actually matters.
    assert decode_json(encode_json(msg)) == decode_msgpack(encode_msgpack(msg))


@pytest.mark.parametrize("msg", sample_messages(), ids=lambda m: f"{m.type.value}-{m.seq}")
def test_the_two_encodings_carry_identical_logical_content(msg):
    import json

    import msgpack

    assert json.loads(encode_json(msg)) == msgpack.unpackb(encode_msgpack(msg), raw=False)


def test_jsonable_form_uses_plain_strings_for_enums():
    m = make_message("status", run_id="r", seq=0, status="SUCCEEDED")
    d = to_jsonable(m)
    assert d["type"] == "status" and d["status"] == "SUCCEEDED"
    assert isinstance(d["type"], str) and isinstance(d["status"], str)


def test_batch_frame_round_trips_in_order():
    msgs = sample_messages()
    assert decode_msgpack_many(encode_msgpack_many(msgs)) == msgs


def test_decoding_garbage_raises_rather_than_returning_a_partial_object():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        decode_json(b'{"type":"log","run_id":"r"}')  # no seq/ts/message
