"""Run a model through the real harness, on your machine or in a notebook.

The point of this module is that there is nothing to set up. No repo root to
find, no `sys.path` to arrange, no environment variables, no Databricks. It
runs the same `Harness` a deployed job runs, writes real telemetry part files
to a directory of your choosing, and hands back the messages.

    from job.local import run_local

    outcome, messages = run_local("heartbeat", seconds=10, hz=2)

    outcome.status          # 'SUCCEEDED'
    len(messages)           # every envelope the run produced
    outcome.telemetry_dir   # the part files, if you want to look

For working on model *logic* you usually want less than this — import the
model and drive it directly, which needs no harness at all:

    from heartbeat import Heartbeat

    m = Heartbeat(seconds=5)
    m.attach(emit=print, should_cancel=lambda: False)
    m.run()

Use `run_local` when the question is about the *run* — does the telemetry look
right, does cancellation land, what does the client actually receive — and the
direct form when the question is about the model.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .harness import Harness, RunOutcome
from .telemetry import PartFileWriter

__all__ = ["run_local", "LocalRun"]


@dataclass
class LocalRun:
    """What a local run produced. `outcome` is the same `RunOutcome` a deployed
    job returns, so anything true here is true there."""

    outcome: RunOutcome
    messages: list[dict[str, Any]]
    telemetry_dir: Path

    def of_type(self, type: str) -> list[dict[str, Any]]:
        """Just the `log`s, or just the `progress` — the usual first question."""
        return [m for m in self.messages if m.get("type") == type]

    def __iter__(self):
        """So `outcome, messages = run_local(...)` reads naturally."""
        return iter((self.outcome, self.messages))


def run_local(
    model: str,
    *,
    run_id: str = "local",
    telemetry_dir: str | Path | None = None,
    roll_every: float = 1.0,
    on_message: Any = None,
    **config: Any,
) -> LocalRun:
    """Drive `model` through the harness with no app and no Databricks.

    `model` is a name (`"heartbeat"`) if the model is installed, or an import
    path (`"mypkg.thing"`) if you are working on one that is not yet.

    `telemetry_dir` defaults to a temp directory. Point it somewhere real when
    you want to keep the part files — they are the same JSONL a deployed run
    writes to the volume, so the ingestion job can be developed against them.

    `on_message` is the live channel a deployed run gives the app. Pass `print`
    to watch messages as they happen; leave it off and read them at the end.
    """
    root = Path(telemetry_dir) if telemetry_dir else Path(tempfile.mkdtemp(prefix="dbx-local-"))
    writer = PartFileWriter(root, run_id)

    harness = Harness(
        run_id,
        writer,
        model_spec=model,
        model_config=config,
        on_message=on_message,
        roll_tick_s=roll_every,
    )
    outcome = harness.run()

    # Read them back from the part files rather than collecting them in
    # memory: this is the same path `replay` and the ingestion job take, so if
    # the durable record is wrong you find out here rather than on a workspace.
    return LocalRun(outcome=outcome, messages=harness.replay(0), telemetry_dir=root)
