"""The core DDL against the rows the job actually writes.

`uc_ddl/README.md` has said since it was written that "column shapes mirror
`shared/tables.py`... a mismatch surfaces at write time on a real workspace and
nowhere in the test suite". The second half was true: `tests/deploy/` bound
models to the registry, to extras, to requirements, to job files, to
`DBX_JOB_IDS` and to the *existence* of a results table, and nothing anywhere
read `001_core_tables.sql`.

That is the same shape as the bug that prompted this audit — a settings file
nothing checked, quietly disagreeing with the code that read it. Here the cost
is higher than a degraded fallback: Spark's `saveAsTable` append is the only
durable write path (`job/delta.py`; delta-rs raises rather than pretending),
so a column the DDL lacks fails the write on a workspace, inside a job, at the
end of a long run. A column the DDL has and `to_row` never fills is the
cheaper direction and still worth knowing about, because it accumulates.

Not covered here, deliberately: the *types*. Matching `BIGINT` to `int` needs
a mapping this file would have to invent, and the failure it would catch —
Spark rejecting a string into a BIGINT — is loud rather than silent. Names are
where drift hides.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from shared.envelope import make_message
from shared.tables import EVENTS, LOGS, PROGRESS, RESULT_META, table_for, to_row

DDL = pathlib.Path(__file__).resolve().parents[2] / "uc_ddl" / "001_core_tables.sql"

#: One fully-populated message per type. Every optional field is set on
#: purpose: a message that omits `primary_metric_label` still produces the key
#: in `to_row`, but building them full keeps this test honest if that ever
#: stops being true.
MESSAGES = [
    make_message(
        "log",
        run_id="r",
        seq=1,
        message="built 840 vars",
        level="INFO",
        source="model",
        phase="build",
        client_visible=True,
    ),
    make_message(
        "progress",
        run_id="r",
        seq=2,
        elapsed_seconds=12.5,
        percent_complete=40.0,
        primary_metric=0.03,
        primary_metric_label="mip_gap",
        payload={"nodes_explored": 900},
    ),
    make_message("status", run_id="r", seq=3, status="RUNNING", detail="solving"),
    make_message(
        "result",
        run_id="r",
        seq=4,
        row_count=7,
        chunk_index=0,
        final=True,
        fetch_hint={"table": "t", "key": "run_id"},
        preview=[{"t": 0, "value": 1.2}],
    ),
]


def ddl_columns() -> dict[str, list[str]]:
    """Table name -> column names, read out of the CREATE TABLE bodies.

    A parser rather than an import because this file is applied by hand with
    `databricks sql query --file` and has never been executed anywhere — there
    is no object to introspect, only the text.
    """
    text = DDL.read_text()
    found: dict[str, list[str]] = {}
    for match in re.finditer(
        r"CREATE TABLE IF NOT EXISTS\s+\S+\.(\w+)\s*\((.*?)\n\)\s*\nUSING DELTA",
        text,
        re.S,
    ):
        table, body = match.group(1), match.group(2)
        columns = []
        for line in body.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("--"):
                continue
            name = stripped.split()[0]
            if name.isidentifier():
                columns.append(name)
        found[table] = columns
    return found


@pytest.fixture(scope="module")
def columns() -> dict[str, list[str]]:
    parsed = ddl_columns()
    # If the parser silently matched nothing, every assertion below would pass
    # vacuously — which is the one way a test like this fails at its job.
    assert set(parsed) >= {LOGS, PROGRESS, EVENTS, RESULT_META, "run_status"}, parsed
    return parsed


@pytest.mark.parametrize("msg", MESSAGES, ids=lambda m: str(m.type))
def test_every_row_key_has_a_column(msg, columns):
    """The direction that costs a run: a dropped field."""
    table = table_for(msg)
    missing = sorted(set(to_row(msg)) - set(columns[table]))
    assert not missing, (
        f"{type(msg).__name__} writes {missing} and {table} has no such column. "
        "Spark's saveAsTable append is the only durable write path, so this "
        "fails the write on a workspace at the end of a run."
    )


@pytest.mark.parametrize("msg", MESSAGES, ids=lambda m: str(m.type))
def test_every_column_is_written(msg, columns):
    """The other direction: a column left behind by a field that was renamed
    or removed. Harmless at write time, which is exactly why it accumulates.
    """
    table = table_for(msg)
    unwritten = sorted(set(columns[table]) - set(to_row(msg)))
    assert not unwritten, f"{table} declares {unwritten}, which to_row never fills"


def test_no_column_is_declared_twice(columns):
    """A copy-paste failure the warehouse would reject and nothing else would."""
    for table, names in columns.items():
        duplicates = sorted({n for n in names if names.count(n) > 1})
        assert not duplicates, f"{table} declares {duplicates} more than once"


def test_run_status_carries_what_the_run_store_reads(columns):
    """`run_status` is the one table here the job never writes, so `to_row`
    says nothing about it. The warehouse-backed store reads it by name, and
    those names are what must line up.
    """
    from server.store import _COLUMN_NAMES

    missing = sorted(set(_COLUMN_NAMES) - set(columns["run_status"]))
    assert not missing, f"WarehouseRunStore selects {missing}, which run_status lacks"


def test_run_status_stays_nullable_where_the_merge_leaves_it_empty(columns):
    """`app/server/repository.py::set_run_status` upserts with only
    (run_id, job_run_id, status, detail, updated_ts).

    The Postgres copy of this table declares `model` and `started_ts` NOT NULL
    and can afford to, because everything reaches it through `claim_slot`.
    Copying that here would turn the MERGE's NOT MATCHED branch into a hard
    failure on a workspace. This test exists so the two schemas diverging is a
    recorded decision rather than something a future tidy-up "fixes".
    """
    body = DDL.read_text()
    table = body[body.index("CREATE TABLE IF NOT EXISTS main.dbx_leaning.run_status") :]
    table = table[: table.index("USING DELTA")]
    for column in ("model", "started_ts"):
        line = next(ln for ln in table.splitlines() if ln.strip().startswith(column))
        assert "NOT NULL" not in line.upper(), (
            f"run_status.{column} is NOT NULL, but repository.py's MERGE does not "
            "supply it on insert"
        )


# ---------------------------------------------------------------------------
# The per-model results tables.
#
# `test_bundle.py` binds each model to a table NAME, in both directions. What
# follows is about the conventions every one of those tables shares. Their
# individual columns are still checked by nothing automated — that needs the
# models imported and run, which pulls gurobipy, torch and ortools into the
# test process for a set of dict keys. `uc_ddl/README.md` says to diff them by
# hand; these tests cover the part that is cheap.
# ---------------------------------------------------------------------------

MODEL_DDL = pathlib.Path(__file__).resolve().parents[2] / "uc_ddl" / "002_model_results.sql"

#: What `Dataset.describe()` puts on every result row of every model, so a run
#: on real `samples` rows and one that fell back to the deterministic generator
#: stay distinguishable from the results table alone.
PROVENANCE = ("data_source", "data_synthetic", "data_rows", "data_fallback_reason")


def results_tables() -> dict[str, list[str]]:
    text = MODEL_DDL.read_text()
    found: dict[str, list[str]] = {}
    for match in re.finditer(
        r"CREATE TABLE IF NOT EXISTS\s+\S+\.(results_\w+)\s*\((.*?)\n\)\s*\nUSING DELTA",
        text,
        re.S,
    ):
        table, body = match.group(1), match.group(2)
        columns = []
        for line in body.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("--"):
                continue
            name = stripped.split()[0]
            if name.isidentifier():
                columns.append((name, "NOT NULL" in stripped.upper()))
        found[table] = columns
    return found


@pytest.fixture(scope="module")
def results() -> dict[str, list[tuple[str, bool]]]:
    parsed = results_tables()
    assert len(parsed) >= 11, sorted(parsed)
    return parsed


def test_every_results_table_starts_with_the_two_stamped_columns(results):
    """`job/emitter.py` prepends `run_id` and `chunk_index` to every row and
    the model must not supply either. Both are therefore always present, which
    is what NOT NULL records — a null in either would mean the harness was
    bypassed. Two of eleven had `chunk_index` nullable, which read as if the
    difference meant something.
    """
    for table, columns in results.items():
        first_two = columns[:2]
        assert [name for name, _ in first_two] == ["run_id", "chunk_index"], (
            f"{table} starts with {[n for n, _ in first_two]}"
        )
        assert all(not_null for _, not_null in first_two), (
            f"{table}: run_id/chunk_index must be NOT NULL — the harness always stamps them"
        )


def test_every_results_table_carries_provenance(results):
    """A missing provenance column is the quiet one: the model still emits the
    key, the write fails, and it fails on a workspace rather than here.
    """
    for table, columns in results.items():
        names = {name for name, _ in columns}
        missing = sorted(set(PROVENANCE) - names)
        assert not missing, f"{table} lacks {missing}; every model emits all four"


def test_provenance_columns_are_nullable(results):
    """`data_fallback_reason` is None on the real-data path by design — that is
    the whole point of it always being present rather than omitted on success.
    """
    for table, columns in results.items():
        for name, not_null in columns:
            if name in PROVENANCE:
                assert not not_null, f"{table}.{name} is NOT NULL but is null on the real path"
