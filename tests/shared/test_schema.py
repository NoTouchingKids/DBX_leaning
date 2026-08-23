"""The generated protocol schema — the frontend's half of the contract.

Two things worth guarding: that the committed files match the models, and
that the schema actually accepts the messages the server really sends. A
schema that is merely *present* is worth very little.
"""

from __future__ import annotations

import json
import pathlib
import subprocess

import pytest

from shared.codec import to_jsonable
from shared.envelope import LogLevel, MessageType, RunStatus, make_message
from shared.protocol import ControlKind, cancel, hello
from shared.schema import SCHEMA_VERSION, control_schema, envelope_schema, protocol_schema

jsonschema = pytest.importorskip("jsonschema")

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCHEMA_DIR = ROOT / "schema"


def validator(schema: dict):
    cls = jsonschema.validators.validator_for(schema)
    cls.check_schema(schema)
    return cls(schema)


# --- the committed files are the models, not a copy of them ----------------


def test_the_committed_schema_matches_the_models():
    result = subprocess.run(
        ["uv", "run", "python", "scripts/export_schema.py", "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "filename", ["envelope.schema.json", "control.schema.json", "protocol.schema.json"]
)
def test_every_committed_file_is_itself_a_valid_schema(filename):
    schema = json.loads((SCHEMA_DIR / filename).read_text())
    jsonschema.validators.validator_for(schema).check_schema(schema)


# --- the schema describes what actually goes on the wire -------------------


def sample_messages():
    return [
        make_message("log", run_id="r", seq=0, message="x", level="ERROR", phase="solve"),
        make_message(
            "progress",
            run_id="r",
            seq=1,
            elapsed_seconds=1.5,
            percent_complete=40.0,
            primary_metric=0.03,
            primary_metric_label="mip_gap",
            payload={"nodes": 12, "incumbent": None},
        ),
        make_message("status", run_id="r", seq=2, status="SUCCEEDED", detail="done"),
        make_message(
            "result",
            run_id="r",
            seq=3,
            row_count=8760,
            preview=[{"t": 1, "v": 2.5}],
            fetch_hint={"table": "main.dbx_leaning.results_x", "key": "run_id"},
            chunk_index=2,
            final=False,
        ),
    ]


@pytest.mark.parametrize("msg", sample_messages(), ids=lambda m: m.type.value)
def test_real_messages_validate_against_the_generated_schema(msg):
    """The exact bytes the SSE endpoint emits, checked against the schema a
    client would validate with."""
    validator(envelope_schema()).validate(to_jsonable(msg))


def test_a_message_missing_a_required_field_is_rejected():
    with pytest.raises(jsonschema.ValidationError):
        validator(envelope_schema()).validate({"type": "log", "run_id": "r", "seq": 0})


def test_an_unknown_message_type_is_rejected():
    with pytest.raises(jsonschema.ValidationError):
        validator(envelope_schema()).validate(
            {"type": "telemetry", "run_id": "r", "seq": 0, "ts": 1}
        )


def test_the_union_is_discriminated_on_type_so_a_client_can_narrow():
    schema = envelope_schema()
    assert schema["discriminator"]["propertyName"] == "type"
    assert set(schema["discriminator"]["mapping"]) == {m.value for m in MessageType}


# --- enums reach the client as enums, not as bare strings ------------------


@pytest.mark.parametrize(
    "name,enum", [("LogLevel", LogLevel), ("RunStatus", RunStatus)]
)
def test_every_enum_member_is_in_the_schema(name, enum):
    """So the frontend gets string-literal unions instead of retyping these
    by hand and going stale the first time one is added."""
    defs = envelope_schema()["$defs"]
    assert defs[name]["enum"] == [m.value for m in enum]
    assert defs[name]["type"] == "string"


def test_the_status_enum_covers_what_the_run_store_can_produce():
    from app.store import TERMINAL_SQL_LIST

    published = set(envelope_schema()["$defs"]["RunStatus"]["enum"])
    for value in TERMINAL_SQL_LIST.replace("'", "").split(", "):
        assert value in published


# --- control frames --------------------------------------------------------


@pytest.mark.parametrize(
    "frame", [hello("r1", next_seq=4000), cancel("r1", requested_by="kp")], ids=["hello", "cancel"]
)
def test_control_frames_validate(frame):
    validator(control_schema()).validate(frame.model_dump(mode="json"))


def test_every_control_kind_is_published():
    schema = control_schema()
    kinds = schema["$defs"]["ControlKind"]["enum"]
    assert set(kinds) == {k.value for k in ControlKind}


# --- versioning ------------------------------------------------------------


def test_the_schema_carries_a_version_a_client_can_compare():
    for build in (envelope_schema, control_schema, protocol_schema):
        assert build()["x-schema-version"] == SCHEMA_VERSION


def test_the_combined_document_holds_both_halves():
    assert set(protocol_schema()["$defs"]) == {"envelope", "control"}
