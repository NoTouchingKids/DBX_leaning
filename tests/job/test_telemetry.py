"""The durable path, and the two properties Slice 0 bought with a workspace run.

Most of this file exists because the probe found that a volume write can
succeed while nothing becomes durable. The tests that matter here are the ones
about *when* a record is readable and *what* replay covers — not the ones about
whether `append` throws.
"""

from __future__ import annotations

import json

from job.telemetry import PartFileWriter


def _rec(seq: int, **extra) -> dict:
    return {"type": "log", "run_id": "r1", "seq": seq, "ts": 1000 + seq, **extra}


def _parts(writer: PartFileWriter) -> list[str]:
    return sorted(p.name for p in writer.run_dir.glob("part-*.jsonl"))


def test_a_record_is_not_durable_until_a_part_closes(tmp_path):
    """The property the whole design turns on.

    Appending does not write. This is not an implementation detail to be
    optimised away later: on a UC volume a held-open handle materialises
    nothing until close, so anything appended and not yet rolled is lost on a
    crash. `unflushed` is that number, and the harness refuses to report
    SUCCEEDED while it is non-zero.
    """
    w = PartFileWriter(tmp_path, "r1", max_bytes=10_000, max_age_s=3600)
    w.append(_rec(0))
    w.append(_rec(1))

    assert _parts(w) == [], "records reached disk before a roll"
    assert w.unflushed == 2
    assert w.rows_written == 0

    w.close()
    assert _parts(w) == ["part-00001.jsonl"]
    assert w.unflushed == 0
    assert w.rows_written == 2


def test_replay_includes_pending_records_not_only_closed_parts(tmp_path):
    """The bug this design would otherwise have shipped.

    Reading replay off the closed part files is the obvious implementation and
    it is wrong in exactly the case replay exists for: a client that just
    reconnected is missing the NEWEST records, which are the ones still
    pending. Closed-parts-only would return the old ones and silently omit
    those.
    """
    w = PartFileWriter(tmp_path, "r1", max_bytes=1, max_age_s=3600)
    w.append(_rec(0))  # max_bytes=1 means this rolls on the next check
    w.roll_if_due()
    w.append(_rec(1))
    w.append(_rec(2))

    assert _parts(w) == ["part-00001.jsonl"], "precondition: 1 and 2 are still pending"

    got = [r["seq"] for r in w.replay(0)]
    assert got == [0, 1, 2], f"replay lost the pending records: {got}"


def test_replay_is_bounded_at_both_ends_and_ordered(tmp_path):
    w = PartFileWriter(tmp_path, "r1", max_bytes=1, max_age_s=3600)
    for seq in range(6):
        w.append(_rec(seq))
        w.roll_if_due()

    assert [r["seq"] for r in w.replay(2, 4)] == [2, 3, 4]
    assert [r["seq"] for r in w.replay(4)] == [4, 5]
    assert w.replay(99) == []


def test_it_rolls_on_size(tmp_path):
    w = PartFileWriter(tmp_path, "r1", max_bytes=120, max_age_s=3600)
    rolled = 0
    for seq in range(10):
        w.append(_rec(seq, message="x" * 40))
        rolled += w.roll_if_due()

    assert rolled >= 2, "a 120-byte cap never rolled over 10 padded records"
    assert w.rows_written > 0


def test_it_rolls_on_age_which_is_the_bound_on_what_a_crash_loses(tmp_path):
    """Size alone is not a durability guarantee: a slow run producing a
    trickle of records would sit below any size cap indefinitely. The age
    bound is the one that caps loss, and it is why `max_age_s` is a priced
    decision rather than a free dial — a close costs ~117ms."""
    w = PartFileWriter(tmp_path, "r1", max_bytes=10_000_000, max_age_s=0)
    w.append(_rec(0))

    assert w.roll_if_due() is True
    assert _parts(w) == ["part-00001.jsonl"]
    assert w.unflushed == 0


def test_rolling_with_nothing_pending_writes_no_empty_part(tmp_path):
    """A roller thread ticks on a timer whether or not anything happened, and
    an idle run must not litter the volume with empty files for the ingestion
    job to read."""
    w = PartFileWriter(tmp_path, "r1", max_age_s=0)
    assert w.roll_if_due() is False
    w.close()
    assert _parts(w) == []


def test_parts_are_numbered_in_order_and_hold_what_went_in(tmp_path):
    w = PartFileWriter(tmp_path, "r1", max_bytes=1, max_age_s=3600)
    for seq in range(3):
        w.append(_rec(seq))
        w.roll_if_due()

    assert _parts(w) == ["part-00001.jsonl", "part-00002.jsonl", "part-00003.jsonl"]

    read_back = []
    for name in _parts(w):
        with open(w.run_dir / name, encoding="utf-8") as fh:
            read_back.extend(json.loads(line) for line in fh if line.strip())
    assert [r["seq"] for r in read_back] == [0, 1, 2]
    assert read_back[0] == _rec(0)


def test_a_failed_write_keeps_the_records_rather_than_dropping_them(tmp_path, monkeypatch):
    """A lost part must stay counted, or the run reports SUCCEEDED over it.

    Putting the batch back is what makes `unflushed` honest and gives a later
    roll a chance to succeed — dropping them would leave the counters looking
    healthy while the data was gone, which is the failure this platform's
    durability rules exist to prevent.
    """
    w = PartFileWriter(tmp_path, "r1", max_bytes=1, max_age_s=3600)
    w.append(_rec(0))

    def boom(*_args, **_kwargs):
        raise OSError("volume unavailable")

    monkeypatch.setattr("pathlib.Path.write_text", boom)
    assert w.roll_if_due() is False

    assert w.unflushed == 1, "a failed write dropped the record"
    assert w.rows_written == 0
    assert w.write_failures == 1
    assert "volume unavailable" in (w.last_error or "")

    # And a later roll, once the volume is back, still writes them.
    monkeypatch.undo()
    assert w.roll_if_due() is True
    assert w.unflushed == 0
    assert [r["seq"] for r in w.replay(0)] == [0]


def test_appends_from_many_threads_are_all_accounted_for(tmp_path):
    """The model thread appends while the roller thread ages parts out."""
    import threading

    w = PartFileWriter(tmp_path, "r1", max_bytes=200, max_age_s=3600)
    stop = threading.Event()

    def roll():
        while not stop.is_set():
            w.roll_if_due()

    roller = threading.Thread(target=roll)
    roller.start()
    try:
        for seq in range(500):
            w.append(_rec(seq))
    finally:
        stop.set()
        roller.join()
    w.close()

    assert w.rows_written == 500
    assert [r["seq"] for r in w.replay(0)] == list(range(500))
