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

    #: What the CHANNEL actually put on a socket — not what the harness handed
    #: it. The two differ whenever the app is unreachable: the queue accepts
    #: every record and delivers none. `outcome.live_offered` is the other
    #: number, and reporting it as delivery would be a metric claiming success
    #: over something that never happened.
    delivered: int = 0

    #: Successful socket opens, and the last error if any. With `delivered`,
    #: these are the three numbers that tell you WHICH way a live run failed:
    #: never connected, connected and sent nothing, or connected and dropped.
    connects: int = 0
    last_error: str | None = None

    @property
    def observed(self) -> bool:
        """Did anything actually reach the app. Not "was a channel configured"."""
        return self.delivered > 0

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
    app_url: str | None = None,
    workspace_host: str | None = None,
    **config: Any,
) -> LocalRun:
    """Drive `model` through the harness, with or without a live channel.

    `model` is a name (`"heartbeat"`) if the model is installed, or an import
    path (`"mypkg.thing"`) if you are working on one that is not yet.

    `telemetry_dir` defaults to a temp directory. Point it somewhere real when
    you want to keep the part files — they are the same JSONL a deployed run
    writes to the volume, so the ingestion job can be developed against them.

    `on_message` is a LOCAL callback. Pass `print` to watch messages as they
    happen; it is not a network and needs nothing configured.

    `app_url` is the real thing: give it the app's public URL and this opens
    the same WebSocket a deployed job opens, with the same credentials, through
    `job.ws.app_client`. That is the point of it being the same function —
    a second wiring that merely looked equivalent would make a notebook a test
    of itself rather than of the job.

        run = run_local("heartbeat", seconds=30, hz=1,
                        app_url="https://dbx-leaning-....databricksapps.com")
        run.observed      # did anything actually ARRIVE
        run.delivered     # how much
        run.last_error    # and if not, why

    **A live channel cannot fail a run.** An unreachable app, a missing grant,
    an expired token: all of them leave the run SUCCEEDED with `delivered == 0`,
    because a run nobody watched is the normal case rather than a broken one.
    So check `observed` — a green status says nothing about the socket.

    There is no credential to pass. The Databricks identity comes from the
    SDK's default chain — in a notebook, that is you — and it is the only one
    the ingress wants; the app's own shared secret is gone. See `job/auth.py`.
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

    client = None
    if app_url:
        from .ws import app_client

        client = app_client(
            app_url,
            run_id,
            on_cancel=harness.cancel,
            on_replay=harness.replay,
            next_seq=lambda: harness.seq.issued,
            workspace_host=workspace_host,
        )
        # Both, when a caller asked for both: `on_message` stays a local view
        # and the socket gets everything too. Silently replacing the callback
        # the caller passed would lose the one they can actually see.
        local = on_message
        harness._on_message = (  # noqa: SLF001 - assembled here, as main.py does
            client.send if local is None else lambda record: (local(record), client.send(record))[1]
        )
        client.start()

    try:
        outcome = harness.run()
    finally:
        if client is not None:
            client.stop()

    # Read them back from the part files rather than collecting them in
    # memory: this is the same path `replay` and the ingestion job take, so if
    # the durable record is wrong you find out here rather than on a workspace.
    return LocalRun(
        outcome=outcome,
        messages=harness.replay(0),
        telemetry_dir=root,
        delivered=client.sent if client is not None else 0,
        connects=client.connects if client is not None else 0,
        last_error=client.last_error if client is not None else None,
    )
