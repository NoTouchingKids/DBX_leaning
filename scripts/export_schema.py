#!/usr/bin/env python3
"""Write the protocol's JSON Schema to schema/, for the frontend to build on.

Generated from the Pydantic models rather than maintained by hand — the
frontend's types and the server's validation come from one definition, so
they cannot drift.

    uv run python scripts/export_schema.py           # write
    uv run python scripts/export_schema.py --check   # verify, write nothing

Downstream, in the frontend (when that track starts):

    npx json-schema-to-typescript schema/envelope.schema.json -o src/protocol.ts
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from shared.schema import control_schema, envelope_schema, protocol_schema  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "schema"

TARGETS = {
    "envelope.schema.json": envelope_schema,
    "control.schema.json": control_schema,
    "protocol.schema.json": protocol_schema,
}


def render(builder) -> str:
    return json.dumps(builder(), indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify only; write nothing")
    args = parser.parse_args()

    OUT_DIR.mkdir(exist_ok=True)
    stale = []
    for filename, builder in TARGETS.items():
        path = OUT_DIR / filename
        content = render(builder)
        if args.check:
            if not path.exists() or path.read_text() != content:
                stale.append(filename)
            continue
        path.write_text(content)
        print(f"wrote schema/{filename}")

    if args.check:
        if stale:
            print(f"out of date: {', '.join(stale)}", file=sys.stderr)
            print("run: uv run python scripts/export_schema.py", file=sys.stderr)
            return 1
        print("schema/ matches the models")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
