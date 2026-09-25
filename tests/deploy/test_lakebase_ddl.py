"""The committed Postgres DDL must be the schema the app checks for.

The `.sql` files are canonical: nothing in `app/` creates a table any more
(docs/v5-implementation-plan.md, Phase 2 item 3). What the app holds instead is
`store.EXPECTED_COLUMNS` / `EXPECTED_KEYS` — what its startup check compares
the live database against. Two descriptions of one schema is how they drift,
so this parses the files and fails the moment they disagree.

This is the static half, and needs no database. The live half —
`tests/app/test_run_store.py::test_the_committed_ddl_passes_the_startup_check`
— applies the files to a real Postgres and runs the check itself.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from server.store import (
    DDL_FILES,
    DEFAULT_SCHEMA,
    EXPECTED_COLUMNS,
    EXPECTED_KEYS,
    Column,
)

ROOT = pathlib.Path(__file__).resolve().parents[2]
DDL_DIR = ROOT / "lakebase_ddl"

#: DDL type -> what information_schema reports, which is what the app compares.
_TYPES = {"TEXT": "text", "BIGINT": "bigint", "BIGSERIAL": "bigint", "BOOLEAN": "boolean"}


def _create_table_body(text: str, table: str) -> str:
    match = re.search(
        rf"CREATE TABLE IF NOT EXISTS {DEFAULT_SCHEMA}\.{table} \((.*?)\n\);",
        text,
        re.DOTALL,
    )
    assert match, f"no CREATE TABLE IF NOT EXISTS {DEFAULT_SCHEMA}.{table} ( ... );"
    return match.group(1)


def _parse(body: str) -> tuple[dict[str, Column], set[tuple[str, ...]]]:
    columns: dict[str, Column] = {}
    keys: set[tuple[str, ...]] = set()
    for raw in body.splitlines():
        line = raw.split("--", 1)[0].strip().rstrip(",")
        if not line:
            continue
        upper = line.upper()
        unique = re.search(r"(?:UNIQUE|PRIMARY KEY)\s*\(([^)]*)\)", line, re.IGNORECASE)
        if upper.startswith(("CONSTRAINT", "UNIQUE", "PRIMARY KEY")):
            assert unique, f"unparsed table constraint: {line!r}"
            keys.add(tuple(c.strip() for c in unique.group(1).split(",")))
            continue
        name, ddl_type, *_ = line.split()
        assert ddl_type.upper() in _TYPES, f"{name}: type {ddl_type} not in the map"
        pk = "PRIMARY KEY" in upper
        if pk:
            keys.add((name,))
        columns[name] = Column(_TYPES[ddl_type.upper()], not (pk or "NOT NULL" in upper))
    return columns, keys


@pytest.mark.parametrize("table", sorted(EXPECTED_COLUMNS))
def test_each_table_matches_what_the_app_checks_for(table):
    path = ROOT / DDL_FILES[table]
    assert path.exists(), f"{DDL_FILES[table]} is named by store.DDL_FILES but missing"
    columns, keys = _parse(_create_table_body(path.read_text(), table))
    assert columns == EXPECTED_COLUMNS[table], (
        f"{DDL_FILES[table]} has drifted from app/server/store.py's EXPECTED_COLUMNS"
    )
    assert EXPECTED_KEYS[table] in keys, (
        f"{DDL_FILES[table]} lacks the ({', '.join(EXPECTED_KEYS[table])}) key "
        "store.REPORT_SQL's ON CONFLICT names"
    )


def test_every_ddl_file_is_one_the_app_knows_about():
    """A third file nobody's check covers is a table the app does not know
    is missing."""
    on_disk = {f"lakebase_ddl/{p.name}" for p in DDL_DIR.glob("*.sql")}
    assert on_disk == set(DDL_FILES.values())


@pytest.mark.parametrize("path", sorted(DDL_DIR.glob("*.sql")), ids=lambda p: p.name)
def test_the_ddl_does_not_use_public(path):
    """`public` grants no CREATE to non-owners since PostgreSQL 15."""
    text = path.read_text().lower()
    assert f"create schema if not exists {DEFAULT_SCHEMA};" in text
    for create in re.findall(r"^create table if not exists (\S+)", text, re.MULTILINE):
        assert create.startswith(f"{DEFAULT_SCHEMA}."), create


@pytest.mark.parametrize("path", sorted(DDL_DIR.glob("*.sql")), ids=lambda p: p.name)
def test_the_ddl_says_it_is_applied_out_of_band_and_how(path):
    """Phase 2 item 3. 001's header used to say startup applied it."""
    text = path.read_text()
    assert "OUT OF BAND" in text
    assert "ensure_schema" not in text, "the app no longer applies DDL; nothing is called that"
    for name in DDL_FILES.values():
        assert f"-f {name}" in text, f"{path.name} does not show the psql command for {name}"


def test_the_ddl_says_which_postgres_it_was_tested_against():
    text = (DDL_DIR / "001_run_status.sql").read_text()
    assert "18" in text and "16" in text, "the version gap must stay documented"


def test_only_run_state_lives_in_postgres():
    """Everything append-only and high-volume stays in Delta."""
    for path in DDL_DIR.glob("*.sql"):
        text = path.read_text().lower()
        for delta_table in ("run_logs", "run_progress", "run_events", "run_results_meta"):
            assert f"table if not exists {DEFAULT_SCHEMA}.{delta_table}" not in text


def test_the_readme_documents_applying_both_files():
    text = (ROOT / "deploy" / "README.md").read_text()
    for name in DDL_FILES.values():
        assert f"-f {name}" in text, f"deploy/README.md does not show applying {name}"
