"""Job configuration, entirely from the environment.

A Databricks Job task passes these as environment variables or as task
parameters mapped to them. Nothing here reaches out to a service to
configure itself — a job must be able to start and run with no app, no
warehouse, and no network beyond its Delta writes.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = ["JobConfig", "WriterKind"]

WriterKind = str  # "auto" | "delta-rs" | "spark" | "jsonl"


def _env_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from None


def _env_float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number, got {raw!r}") from None


@dataclass(frozen=True)
class JobConfig:
    #: The platform's run identifier — what every message is keyed by, and
    #: what a browser subscribes to. Distinct from Databricks' own job run id.
    run_id: str
    #: Import spec for the model: "job.models.scenario" or "job.models.scenario:build_model".
    model_spec: str
    model_config: dict[str, Any] = field(default_factory=dict)

    #: Databricks' own run id, for reconciliation against the Jobs API.
    job_run_id: str | None = None

    #: Absent = no live channel at all. That is a normal case, not an error:
    #: apps run ~8h/day and jobs do not share that schedule.
    app_url: str | None = None
    app_token: str | None = None

    #: The workspace, for the OAuth exchange in `job/auth.py`.
    #:
    #: `app_token` is the app's OWN check and is not a Databricks identity;
    #: the Apps proxy in front of the app rejects anything that is not an OAuth
    #: token, so the two travel on different headers. Absent, the job falls
    #: back to whatever identity the runtime already gives it.
    workspace_host: str | None = None

    catalog: str = "main"
    schema: str = "dbx_leaning"
    #: Where this model's result rows go. Unqualified names get the
    #: catalog/schema above.
    results_table: str | None = None

    writer: WriterKind = "auto"
    #: Only used by the jsonl writer (local development and tests).
    local_root: str = ".delta-local"

    flush_max_bytes: int = 1_000_000
    #: The bound that actually caps data loss on a crash. Size alone is not a
    #: durability guarantee — a slow run may never reach 1 MB.
    flush_max_age_s: float = 30.0
    flush_tick_s: float = 1.0

    #: Live path only. Full queue drops logs (best-effort by contract);
    #: progress/status/result are never dropped. Durable writes do not pass
    #: through here at all.
    live_queue_max: int = 2000
    ws_reconnect_s: float = 30.0
    ws_ping_s: float = 20.0
    http_push_batch: int = 50
    http_timeout_s: float = 10.0

    #: How often the model's blocking call should look at the cancel flag.
    cancel_poll_s: float = 0.5

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> JobConfig:
        e: Mapping[str, str] = os.environ if env is None else env

        raw_config = e.get("DBX_MODEL_CONFIG", "").strip()
        try:
            model_config = json.loads(raw_config) if raw_config else {}
        except json.JSONDecodeError as exc:
            raise ValueError(f"DBX_MODEL_CONFIG is not valid JSON: {exc}") from None
        if not isinstance(model_config, dict):
            raise ValueError("DBX_MODEL_CONFIG must be a JSON object")

        model_spec = e.get("DBX_MODEL", "").strip()
        if not model_spec:
            raise ValueError(
                "DBX_MODEL is required — the import spec for the model to run, "
                "e.g. 'job.models.scenario' or 'job.models.scenario:build_model'"
            )

        app_url = (e.get("DBX_APP_URL") or "").strip() or None

        return cls(
            run_id=(e.get("DBX_RUN_ID") or "").strip() or f"run-{uuid.uuid4().hex[:12]}",
            model_spec=model_spec,
            model_config=model_config,
            job_run_id=(e.get("DATABRICKS_JOB_RUN_ID") or "").strip() or None,
            app_url=app_url.rstrip("/") if app_url else None,
            app_token=(e.get("DBX_APP_TOKEN") or "").strip() or None,
            workspace_host=(e.get("DATABRICKS_HOST") or e.get("DBX_WORKSPACE_HOST") or "").strip()
            or None,
            catalog=e.get("DBX_CATALOG", "main"),
            schema=e.get("DBX_SCHEMA", "dbx_leaning"),
            results_table=(e.get("DBX_RESULTS_TABLE") or "").strip() or None,
            writer=e.get("DBX_WRITER", "auto"),
            local_root=e.get("DBX_LOCAL_ROOT", ".delta-local"),
            flush_max_bytes=_env_int(e, "DBX_FLUSH_MAX_BYTES", 1_000_000),
            flush_max_age_s=_env_float(e, "DBX_FLUSH_MAX_AGE_S", 30.0),
            flush_tick_s=_env_float(e, "DBX_FLUSH_TICK_S", 1.0),
            live_queue_max=_env_int(e, "DBX_LIVE_QUEUE_MAX", 2000),
            ws_reconnect_s=_env_float(e, "DBX_WS_RECONNECT_S", 30.0),
            ws_ping_s=_env_float(e, "DBX_WS_PING_S", 20.0),
            http_push_batch=_env_int(e, "DBX_HTTP_PUSH_BATCH", 50),
            http_timeout_s=_env_float(e, "DBX_HTTP_TIMEOUT_S", 10.0),
            cancel_poll_s=_env_float(e, "DBX_CANCEL_POLL_S", 0.5),
        )

    @property
    def ws_url(self) -> str | None:
        if not self.app_url:
            return None
        base = self.app_url.replace("https://", "wss://").replace("http://", "ws://")
        return f"{base}/ws/job/{self.run_id}"

    @property
    def push_url(self) -> str | None:
        return f"{self.app_url}/api/runs/{self.run_id}/push" if self.app_url else None
