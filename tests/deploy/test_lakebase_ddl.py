"""The committed Postgres DDL must be the schema the app actually applies.

Two copies of a schema is how they drift; this makes the file a rendering of
app/server/store.py rather than a second source of truth.
"""

from __future__ import annotations

import pathlib

from server.store import SCHEMA_SQL

DDL = pathlib.Path(__file__).resolve().parents[2] / "lakebase_ddl" / "001_run_status.sql"


def test_the_committed_ddl_matches_what_the_app_applies():
    assert DDL.exists()
    assert SCHEMA_SQL.strip() in DDL.read_text(), (
        "lakebase_ddl/001_run_status.sql has drifted from app/server/store.py's SCHEMA_SQL"
    )


def test_the_ddl_says_which_postgres_it_was_tested_against():
    text = DDL.read_text()
    assert "18" in text and "16" in text, "the version gap must stay documented"


def test_only_run_status_lives_in_postgres():
    """Everything append-only stays in Delta; Lakebase Free Edition would not
    thank us for the telemetry volume."""
    text = DDL.read_text().lower()
    for delta_table in ("run_logs", "run_progress", "run_events", "run_results_meta"):
        assert f"table if not exists {delta_table}" not in text
