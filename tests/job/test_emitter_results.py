"""``Emitter._absorb_result_rows`` — the per-chunk preview, and the chunk accounting.

**Why this file exists.** The `result` preview is built *per emission*, from
that emission's rows and nothing else, and the only thing that ever exercised
that combination was `job/models/streaming_results` — a chunked model that
also declared `preview_axes`. That model is gone: `job/models/panel_fit`
covers the chunk contract more thoroughly, but it declares **no**
`preview_axes` on purpose, so it only ever reaches the even-sampling fallback
and never the LTTB branch.

Keeping a deployed job (a job definition, a serverless environment, a
requirements file, a results table) alive to assert something about the
harness is the wrong shape. The assertion belongs to the harness, so it lives
here.

Envelopes are built through `job.shared.*`, never `shared.*` — byte-identical
source, distinct types (see CLAUDE.md).
"""

from __future__ import annotations

import pytest

from job.buffer import DurableBuffer
from job.delta import JsonlWriter
from job.emitter import Emitter
from job.record import RunRecord
from job.shared.tables import TableSet
from job.sink import DurableSink

RESULTS_TABLE = "results_chunked"


def make_emitter(tmp_path, **kw) -> tuple[Emitter, DurableSink]:
    """A real sink and a real buffer, so the qualified name that reaches
    `fetch_hint` is the one a deployment would see, not a stub's guess."""
    sink = DurableSink(JsonlWriter(tmp_path), TableSet(), buffer=DurableBuffer())
    emitter = Emitter(
        "run-1",
        sink=sink,
        record=RunRecord("run-1"),
        results_table=kw.pop("results_table", RESULTS_TABLE),
        **kw,
    )
    return emitter, sink


def series(chunk: int, n: int, *, spike_at: int | None = None) -> list[dict[str, float]]:
    """A time-series-shaped chunk. `t` is globally unique across chunks so a
    preview row can be traced back to the emission it came from."""
    rows = [
        {"chunk": float(chunk), "t": float(chunk * 1000 + i), "v": float((i * 37) % 101)}
        for i in range(n)
    ]
    if spike_at is not None:
        rows[spike_at]["v"] = 5000.0
    return rows


def written_rows(sink: DurableSink, table: str = RESULTS_TABLE) -> list[dict]:
    return sink.buffer.take_all().get(sink.tables.qualify(table), [])


# --- the preview, per chunk -----------------------------------------------


def test_each_chunk_gets_its_own_preview_built_from_that_chunks_rows_alone(tmp_path):
    """The failure this prevents: a preview accumulated across emissions, so
    chunk 2's chart shows chunk 0's data — or the whole run's, growing until
    it hits the envelope's `PREVIEW_MAX_POINTS` ceiling and the message is
    rejected at the end of a long run."""
    emitter, _ = make_emitter(tmp_path, preview_axes=("t", "v"), preview_points=10)

    chunks = [series(c, 50) for c in range(3)]
    messages = [emitter.emit("result", rows=rows, final=(c == 2)) for c, rows in enumerate(chunks)]

    assert len(messages) == 3
    for index, (msg, rows) in enumerate(zip(messages, chunks, strict=True)):
        assert msg.preview, "every emission gets a preview, not just the first or the last"
        # 50 rows into a 10-point preview: downsampling definitely happened,
        # so "the preview is just the rows" cannot make this pass vacuously.
        assert len(msg.preview) <= 10 < len(rows)
        assert {row["chunk"] for row in msg.preview} == {float(index)}, (
            "a preview carried rows from another chunk"
        )
        assert all(row in rows for row in msg.preview)


def test_a_chunk_longer_than_the_preview_bound_keeps_its_first_and_last_row(tmp_path):
    """LTTB's endpoint guarantee, through the emitter. A chart whose first or
    last point moved is a chart lying about where the run started and ended."""
    emitter, _ = make_emitter(tmp_path, preview_axes=("t", "v"), preview_points=100)
    rows = series(0, 600)

    msg = emitter.emit("result", rows=rows)

    assert len(msg.preview) <= 100
    assert msg.preview[0] == rows[0]
    assert msg.preview[-1] == rows[-1]


def test_the_previews_axes_are_what_keeps_a_spike_the_even_fallback_drops(tmp_path):
    """Why `preview_axes` is worth declaring at all.

    Stride sampling hides a spike exactly where it matters — a forecast error
    blow-up landing between two kept points and vanishing. Same rows, same
    bound, one emitter with axes and one without: only the LTTB one keeps the
    spike.
    """
    rows = [{"t": float(i), "v": 1.0} for i in range(21)]
    rows[7]["v"] = 500.0  # even spacing at 5 points picks 0/5/10/15/20 — never 7

    with_axes, _ = make_emitter(tmp_path / "a", preview_axes=("t", "v"), preview_points=5)
    without_axes, _ = make_emitter(tmp_path / "b", preview_points=5)

    lttb = with_axes.emit("result", rows=rows).preview
    even = without_axes.emit("result", rows=rows).preview

    assert rows[7] in lttb, "LTTB dropped the one point the preview exists to show"
    assert rows[7] not in even, "the fallback was expected to miss it; this test is stale"


def test_a_model_with_no_preview_axes_falls_back_to_even_sampling(tmp_path):
    """Not every result set is a series. `panel_fit` emits one row per group,
    where there is no shape for LTTB to preserve — that must downsample, not
    fail and not skip the preview."""
    emitter, _ = make_emitter(tmp_path, preview_points=5)
    rows = [{"group": f"g{i}", "status": "fitted"} for i in range(20)]

    msg = emitter.emit("result", rows=rows)

    # Evenly spaced picks, endpoints included — the arithmetic in
    # `shared.downsample.downsample_rows`, not an approximation of it.
    assert [row["group"] for row in msg.preview] == ["g0", "g5", "g10", "g14", "g19"]


def test_axes_naming_columns_that_are_not_numeric_still_produce_a_preview(tmp_path):
    """A model whose axes went stale (a renamed column, a null-heavy metric)
    must degrade to even sampling rather than raise into model code halfway
    through a run that has already produced results."""
    emitter, _ = make_emitter(tmp_path, preview_axes=("group", "r_squared"), preview_points=5)
    rows = [{"group": f"g{i}", "r_squared": None} for i in range(20)]

    msg = emitter.emit("result", rows=rows)

    assert [row["group"] for row in msg.preview] == ["g0", "g5", "g10", "g14", "g19"]


# --- the chunk accounting -------------------------------------------------


def test_chunk_index_increments_and_row_count_is_that_chunks_own_count(tmp_path):
    """`row_count` is per-chunk, never a running total — the envelope spec is
    explicit, and a running total would make "succeeded, wrote 0 rows"
    unreadable on every chunk after the first."""
    emitter, _ = make_emitter(tmp_path, preview_axes=("t", "v"))
    sizes = [3, 5, 2]

    messages = [emitter.emit("result", rows=series(c, n)) for c, n in enumerate(sizes)]

    assert [m.chunk_index for m in messages] == [0, 1, 2]
    assert [m.row_count for m in messages] == sizes
    assert [m.row_count for m in messages] != [3, 8, 10], "row_count became a running total"
    # `seq` counts every message of every type and is deliberately NOT the
    # chunk index; with only results emitted here they happen to coincide,
    # which is why the two are asserted separately rather than against
    # each other.
    assert emitter.result_chunks == 3
    assert emitter.result_rows_accepted == sum(sizes)


def test_the_rows_never_travel_on_the_message_only_to_the_results_table(tmp_path):
    """The wire contract carries a pointer and a preview, never the result
    set. A model handing over 100k rows must not put them on a socket."""
    emitter, sink = make_emitter(tmp_path, preview_axes=("t", "v"), preview_points=4)
    rows = series(0, 20)

    msg = emitter.emit("result", rows=rows)

    assert not hasattr(msg, "rows")
    assert msg.fetch_hint == {"table": "main.dbx_leaning.results_chunked", "key": "run_id"}
    # All twenty reached the durable table, stamped with the run and the chunk
    # the harness assigned — neither of which the model supplies.
    stored = written_rows(sink)
    assert len(stored) == 20
    assert all(row["run_id"] == "run-1" and row["chunk_index"] == 0 for row in stored)


def test_a_model_declaring_a_row_count_that_disagrees_with_its_rows_is_rejected(tmp_path):
    """The harness counts what it actually wrote; a model that also guesses is
    a second source of truth, and the one that reaches the client would be the
    guess. This raises into model code on purpose — it is the model's own bug,
    and swallowing it is how `row_count` stops meaning "rows written"."""
    emitter, sink = make_emitter(tmp_path, preview_axes=("t", "v"))

    with pytest.raises(ValueError, match="row_count=99 but carries 3 rows"):
        emitter.emit("result", rows=series(0, 3), row_count=99)

    # Rejected before any state moved: no chunk index burned, no seq consumed,
    # no half-written rows. A model that catches the error and re-emits
    # correctly must not leave a hole behind it.
    assert emitter.result_chunks == 0 and emitter.result_rows_accepted == 0
    assert written_rows(sink) == []
    assert emitter.emit("result", rows=series(0, 3)).seq == 0


def test_result_rows_with_no_results_table_configured_say_what_to_set(tmp_path):
    """A model that streams rows into a harness that has nowhere to put them
    must fail loudly and name the fix, not drop them."""
    emitter, _ = make_emitter(tmp_path, results_table=None)

    with pytest.raises(ValueError, match="DBX_RESULTS_TABLE"):
        emitter.emit("result", rows=series(0, 3))
