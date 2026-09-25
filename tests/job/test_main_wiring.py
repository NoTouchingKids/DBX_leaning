"""How `job/main.py` and `run_local` assemble the harness's slots.

The slots are only worth having if the two real entry points fill them: the
Lakebase writer from `job.lakebase.from_config`, and the socket as the channel
— started and closed by the harness, not by the caller.
"""

from __future__ import annotations

import json
import signal
import threading

import pytest

from job.local import run_local


@pytest.fixture(autouse=True)
def _restore_signal_handlers():
    """`main()` installs SIGTERM/SIGINT handlers bound to its harness; put the
    test process's own back afterwards."""
    saved = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    yield
    for sig, handler in saved.items():
        signal.signal(sig, handler)


class Recording:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.threads: set[str] = set()
        self.closed = False

    def write(self, run_id, seq, status, terminal, detail, ts):
        self.threads.add(threading.current_thread().name)
        self.calls.append((run_id, seq, status, terminal, detail, ts))
        return True

    def close(self):
        self.closed = True


def test_main_plugs_the_lakebase_writer_into_the_slot(tmp_path, monkeypatch):
    rec = Recording()
    seen_cfg: list = []

    def fake_from_config(cfg, **_kw):
        seen_cfg.append(cfg)
        return rec

    monkeypatch.setattr("job.lakebase.from_config", fake_from_config)
    for key, value in {
        "DBX_RUN_ID": "wired-1",
        "DBX_MODEL": "heartbeat",
        "DBX_MODEL_CONFIG": json.dumps({"seconds": 0.1, "hz": 20}),
        "DBX_TELEMETRY_VOLUME": str(tmp_path),
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("DBX_APP_URL", raising=False)

    from job.main import main

    assert main() == 0
    assert seen_cfg and seen_cfg[0].model_spec == "heartbeat"
    assert [c[2] for c in rec.calls] == ["RUNNING", "SUCCEEDED"]
    assert [c[3] for c in rec.calls] == [False, True]
    assert rec.threads == {"controller"}
    assert rec.closed, "the harness did not close the status writer at the end of the run"


def test_main_runs_without_a_lakebase_writer(tmp_path, monkeypatch):
    monkeypatch.setattr("job.lakebase.from_config", lambda cfg, **_kw: None)
    for key, value in {
        "DBX_RUN_ID": "wired-2",
        "DBX_MODEL": "heartbeat",
        "DBX_MODEL_CONFIG": json.dumps({"seconds": 0.1, "hz": 20}),
        "DBX_TELEMETRY_VOLUME": str(tmp_path),
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("DBX_APP_URL", raising=False)

    from job.main import main

    assert main() == 0


def test_run_local_takes_a_status_writer(tmp_path):
    rec = Recording()
    run = run_local("heartbeat", seconds=0.1, hz=20, telemetry_dir=tmp_path, status_writer=rec)

    assert run.outcome.status == "SUCCEEDED"
    statuses = run.of_type("status")
    assert [c[1] for c in rec.calls] == [m["seq"] for m in statuses]
    assert rec.closed
