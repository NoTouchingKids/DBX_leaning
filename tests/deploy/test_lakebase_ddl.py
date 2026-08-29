"""The committed Postgres DDL must be the schema the app actually applies.

Two copies of a schema is how they drift; this makes the files a rendering of
app/server/store.py rather than a second source of truth.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from server.store import DEFAULT_SCHEMA, history_schema_sql, schema_sql

LAKEBASE_DDL = pathlib.Path(__file__).resolve().parents[2] / "lakebase_ddl"
DDL = LAKEBASE_DDL / "001_run_status.sql"
HISTORY_DDL = LAKEBASE_DDL / "002_run_status_history.sql"


def test_the_committed_ddl_matches_what_the_app_applies():
    assert DDL.exists()
    assert schema_sql(DEFAULT_SCHEMA).strip() in DDL.read_text(), (
        "lakebase_ddl/001_run_status.sql has drifted from app/server/store.py's schema_sql()"
    )


def test_the_committed_history_ddl_matches_what_the_app_applies():
    assert HISTORY_DDL.exists()
    assert history_schema_sql(DEFAULT_SCHEMA).strip() in HISTORY_DDL.read_text(), (
        "lakebase_ddl/002_run_status_history.sql has drifted from "
        "app/server/store.py's history_schema_sql()"
    )


def test_the_ddl_does_not_put_run_status_in_public():
    """The whole point of the schema: `public` grants no CREATE to non-owners
    since PostgreSQL 15, so a service principal cannot create a table there."""
    text = DDL.read_text().lower()
    assert f"create schema if not exists {DEFAULT_SCHEMA}" in text
    assert f"create table if not exists {DEFAULT_SCHEMA}.run_status" in text
    assert "create table if not exists run_status" not in text


def test_the_history_ddl_does_not_put_its_table_in_public_either():
    """Same failure, second table.

    Note the substring trap this avoids: `dbx_leaning.run_status` is a prefix
    of `dbx_leaning.run_status_history`, so the assertion above passes against
    a file containing only the history table. These name it in full.
    """
    text = HISTORY_DDL.read_text().lower()
    assert f"create schema if not exists {DEFAULT_SCHEMA}" in text, (
        "002 must create the schema itself; deploy/README.md applies files one at a time"
    )
    assert f"create table if not exists {DEFAULT_SCHEMA}.run_status_history" in text
    assert "create table if not exists run_status_history" not in text
    # An index inherits nothing from the CREATE TABLE: an unqualified one
    # resolves through search_path, which this store never sets.
    assert text.count(f"on {DEFAULT_SCHEMA}.run_status_history") == 2, (
        "both indexes must name the schema-qualified table"
    )


def test_the_ddl_says_which_postgres_it_was_tested_against():
    text = DDL.read_text()
    assert "18" in text and "16" in text, "the version gap must stay documented"


@pytest.mark.parametrize("path", [DDL, HISTORY_DDL], ids=lambda p: p.name)
def test_no_telemetry_table_lives_in_postgres(path):
    """Run state and its transitions live here; the telemetry does not.

    `run_status_history` is append-only too, so "append-only means Delta" is no
    longer the line — volume is. A handful of transitions per run is nothing;
    logs, progress and results are thousands of rows each and would cost
    Lakebase Free Edition dearly.
    """
    text = path.read_text().lower()
    for delta_table in ("run_logs", "run_progress", "run_events", "run_results_meta"):
        assert f"table if not exists {delta_table}" not in text


def test_run_status_keeps_the_primary_key_the_history_table_exists_to_protect():
    """The simplification this split refuses.

    Appending transitions to `run_status` itself costs the PRIMARY KEY on
    `run_id`, and with it `ON CONFLICT (run_id) DO UPDATE` — the job's upsert
    and this store's set_status both target it — and the ability to refuse a
    duplicate run_id at all, which is the exact Delta failure the move to
    Postgres fixed. It also turns claim_slot's ceiling count into a
    latest-row-per-run subquery. If this assertion is what is failing, the
    change under it is the wrong one.
    """
    assert re.search(r"run_id\s+TEXT\s+PRIMARY KEY", DDL.read_text()), (
        "run_status lost its primary key on run_id"
    )


def test_the_history_table_is_not_keyed_on_run_id():
    """The mirror of the above: a PK on `run_id` here collapses the log back
    to one row per run, which is the thing `run_status` already is."""
    text = HISTORY_DDL.read_text()
    assert not re.search(r"run_id\s+TEXT\s+.*PRIMARY KEY", text)
    assert re.search(r"id\s+BIGSERIAL PRIMARY KEY", text), (
        "the surrogate key is what orders transitions sharing an epoch-ms ts"
    )


def test_a_repeat_of_the_same_status_message_cannot_be_appended_twice():
    """A reconnecting job re-reports; `seq` is what identifies the message.

    Partial on `seq IS NOT NULL`, because a writer with no envelope behind it
    — the app, at slot-claim time — has no seq and must still be able to
    append. A plain UNIQUE would let exactly one such row exist per run.
    """
    text = HISTORY_DDL.read_text()
    assert re.search(
        r"CREATE UNIQUE INDEX IF NOT EXISTS run_status_history_seq_idx\s+ON \S+ \(run_id, seq\)"
        r" WHERE seq IS NOT NULL",
        text,
    )
    assert not re.search(r"seq\s+BIGINT\s+NOT NULL", text), (
        "seq must stay nullable or the app cannot append at slot-claim time"
    )


def test_seq_and_ts_are_integers_not_text():
    """The lexicographic bug, in schema form: compared as strings "2" > "12"."""
    text = HISTORY_DDL.read_text()
    assert re.search(r"seq\s+BIGINT", text) and re.search(r"ts\s+BIGINT", text)
