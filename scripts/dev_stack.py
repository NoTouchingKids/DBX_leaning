"""Run the whole platform locally, with no Databricks workspace.

    uv run python scripts/dev_stack.py

Then, in another terminal, ``cd app/client && bun run dev`` and open the Vite URL.
Clicking Run on any model triggers the real ``POST /api/runs``, which launches
the real ``job/`` harness in its own process, which attaches to the real
WebSocket ingress and streams real envelope messages to the browser over the
real SSE endpoint.

This is a development loop, not a simulator. What matters about it is which
parts are the shipped code and which are substituted, so:

REAL — the same code that would run on Databricks, unmodified
  * ``app/`` under uvicorn, including SSE, the WS ingress, the HTTP-push
    ingress, cancel forwarding, the ServiceHub and its degradation reporting.
  * ``job/`` in a separate OS process per run, launched through
    ``entrypoints/run_model.py`` with ``KEY=VALUE`` argv exactly as a
    serverless task passes them.
  * ``shared/`` — one envelope, one seq counter per run, msgpack on the wire.
  * The models themselves. Real solving, real progress, real results.
  * The run registry: **Lakebase is plain Postgres**, so an embedded Postgres
    (``pgserver``) gives the same ``PostgresRunStore`` a deploy uses — same
    primary key, same advisory-lock count-and-claim. The concurrency ceiling
    and its 429 are therefore genuinely exercised, not stubbed.
  * The job's ingress token (``DBX_APP_TOKEN``), so the auth path is live.

SUBSTITUTED — and each one is a place local behaviour can diverge
  * **The Jobs API.** ``scripts/dev_launcher.py`` answers ``run-now`` and
    ``runs/get`` and spawns a subprocess. ``DATABRICKS_HOST`` points at it.
    See that module's docstring for exactly what it does and does not
    reproduce; the headline omissions are per-model environments, the
    account's own 5-task ceiling behind the app's check, and serverless
    startup latency.
  * **The durable writer.** ``DBX_WRITER=jsonl`` with
    ``DBX_ALLOW_LOCAL_WRITER=1``: telemetry lands in newline-delimited JSON
    under the state directory, **not** in Unity Catalog. ``job/delta.py``
    requires that opt-in precisely so nobody can be confused about which one
    happened. delta-rs is still unimplemented and still must not be selected.
  * **No SQL warehouse.** So the app runs with ``sql`` degraded:
    ``GET /api/runs/{id}/messages`` (backfill) and ``GET /api/runs/{id}/results``
    answer 503, and startup reconciliation is skipped. Live streaming, trigger,
    cancel, listing and status are all unaffected. ``GET /healthz`` reports
    this rather than hiding it — expect ``"status": "degraded"`` locally, and
    read the reason.
  * **Crash reporting.** Because reconciliation cannot run, the launcher
    reports a job that died without a terminal status as ``FAILED``, over the
    real push ingress. A deploy learns the same fact from the Jobs API at
    startup instead.

Nothing is written inside the repository: state lives under
``~/.cache/dbx-leaning/dev-stack`` (``--state-dir`` to move it, ``--reset`` to
wipe it).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import pathlib
import shutil
import signal
import socket
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from scripts._registry import REPO_ROOT, model_names  # noqa: E402

# scripts.dev_launcher is imported lazily, after preflight: it needs fastapi at
# module level, and "ImportError: No module named fastapi" is a worse first
# experience than the preflight list that names the uv command to fix it.

__all__ = ["DevStack", "build_parser", "default_state_dir", "main", "preflight"]

#: Vite's dev proxy targets 127.0.0.1:8000 for /api and /ws (app/client/vite.config.ts).
#: Changing this means changing that, so it is a default rather than a constant.
DEFAULT_APP_PORT = 8000
DEFAULT_LAUNCHER_PORT = 8787

#: A fixed, obviously-local shared secret. Fixed rather than random so a job
#: log or a curl command a developer copies out of one session still works in
#: the next; obviously-local so it can never be mistaken for a real one.
DEV_JOB_TOKEN = "dev-local-job-token"

#: Cheap models, in the order worth trying first. bayesian_ab finishes in
#: milliseconds, which is the real test of whether an observer can attach at
#: all — the harness starts the run without waiting for the WebSocket, so a run
#: this short is normally delivered over the HTTP-push tier instead. That is
#: correct behaviour, and it is worth seeing it happen.
FAST_MODELS = ("bayesian_ab", "annealing", "scenario")


class StackError(RuntimeError):
    """A prerequisite is missing. Always carries what to do about it."""


def default_state_dir() -> pathlib.Path:
    base = os.environ.get("XDG_CACHE_HOME") or (pathlib.Path.home() / ".cache")
    return pathlib.Path(base) / "dbx-leaning" / "dev-stack"


# --------------------------------------------------------------------------
# Preflight. Every failure here has to name the fix, and none of them may hang.
# --------------------------------------------------------------------------

#: import name -> what to run if it is missing.
REQUIRED_IMPORTS = {
    "fastapi": "uv sync --extra app",
    "uvicorn": "uv sync --extra app",
    "httpx": "uv sync --extra app",
    "psycopg": "uv sync --extra app",
    "websockets": "uv sync --extra job",
    "pgserver": "uv sync   (it is in the dev group)",
}


def preflight(*, ports: dict[str, int], check_ports: bool = True) -> list[str]:
    """Everything that must be true before anything is started.

    Collected and reported together rather than failing on the first one: a
    developer setting this up for the first time should get one list, not six
    consecutive runs.
    """
    import importlib.util

    problems: list[str] = []

    for module, fix in REQUIRED_IMPORTS.items():
        if importlib.util.find_spec(module) is None:
            problems.append(f"{module} is not installed — {fix} (or `uv sync --all-extras`)")

    entrypoint = REPO_ROOT / "entrypoints" / "run_model.py"
    if not entrypoint.exists():
        problems.append(f"{entrypoint} is missing; the launcher has nothing to run")

    if check_ports:
        for label, port in ports.items():
            if _port_in_use(port):
                problems.append(
                    f"port {port} ({label}) is already in use — stop whatever holds it, "
                    f"or pass --{label}-port"
                )
    return problems


def _port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return True
    return False


# --------------------------------------------------------------------------
# The stack
# --------------------------------------------------------------------------


class DevStack:
    """Supervises embedded Postgres, the job launcher and the app."""

    def __init__(
        self,
        *,
        state_dir: pathlib.Path,
        app_port: int = DEFAULT_APP_PORT,
        launcher_port: int = DEFAULT_LAUNCHER_PORT,
        models: list[str] | None = None,
        max_concurrent_runs: int = 5,
        ws_reconnect_s: float = 5.0,
        reload: bool = False,
        quiet_jobs: bool = False,
    ) -> None:
        self.state_dir = state_dir
        self.app_port = app_port
        self.launcher_port = launcher_port
        from scripts.dev_launcher import dev_job_ids

        self.job_ids = dev_job_ids(models)
        self.max_concurrent_runs = max_concurrent_runs
        self.ws_reconnect_s = ws_reconnect_s
        self.reload = reload
        self.quiet_jobs = quiet_jobs
        self.dsn: str | None = None
        self._postgres = None
        self._children: list[tuple[str, subprocess.Popen]] = []
        self._spawned_at: dict[str, float] = {}
        self._app_restarts = 0

    @property
    def app_url(self) -> str:
        return f"http://127.0.0.1:{self.app_port}"

    @property
    def launcher_url(self) -> str:
        return f"http://127.0.0.1:{self.launcher_port}"

    # --- Postgres ---------------------------------------------------------

    def start_postgres(self) -> str:
        """Embedded Postgres, standing in for Lakebase.

        Not a substitution in the way the Jobs API is: Lakebase *is* Postgres,
        so ``PostgresRunStore`` runs here byte for byte — advisory lock,
        primary key and all. Only the credential story differs (a real one
        authenticates with a short-lived OAuth token).
        """
        import pgserver

        directory = self.state_dir / "pg"
        directory.mkdir(parents=True, exist_ok=True)
        self._postgres = pgserver.get_server(directory)
        self.dsn = self._postgres.get_uri()
        return self.dsn

    def reconcile_stale_runs(self) -> int:
        """Fail every run left non-terminal by a previous stack session.

        Locally this is not a guess: the launcher owns every job process, so
        nothing from a previous session can still be running. A deploy learns
        the same thing from the Jobs API in ``app/server/reconcile.py`` — which cannot
        run here, because it needs the warehouse read path.

        Without this, five crashed runs from yesterday hold the whole
        concurrency ceiling and every trigger answers 429.
        """
        from server.store import PostgresRunStore
        from shared.envelope import RunStatus

        async def run() -> int:
            store = PostgresRunStore(self.dsn or "")
            await store.ensure_schema()
            stale = await store.non_terminal(limit=500)
            for record in stale:
                await store.set_status(
                    record.run_id,
                    RunStatus.FAILED,
                    detail="the dev stack restarted; this run's process is gone",
                )
            return len(stale)

        return asyncio.run(run())

    # --- child processes --------------------------------------------------

    def launcher_command(self) -> list[str]:
        return [
            sys.executable,
            str(REPO_ROOT / "scripts" / "dev_launcher.py"),
            "--port",
            str(self.launcher_port),
            "--state-dir",
            str(self.state_dir),
            "--app-url",
            self.app_url,
            "--app-token",
            DEV_JOB_TOKEN,
            *(["--quiet-jobs"] if self.quiet_jobs else []),
        ]

    def launcher_env(self) -> dict[str, str]:
        env = dict(os.environ)
        # Reaches the spawned job. The production default is 30s, which is
        # right for a job that may outlive the app by hours and wrong for a
        # developer who just restarted uvicorn.
        env["DBX_WS_RECONNECT_S"] = str(self.ws_reconnect_s)
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONPATH"] = str(REPO_ROOT)
        return env

    def app_command(self) -> list[str]:
        return [
            sys.executable,
            "-m",
            "uvicorn",
            "server.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(self.app_port),
            *(["--reload"] if self.reload else []),
        ]

    def app_env(self) -> dict[str, str]:
        """Exactly the variables a deployed app reads — no local-only switches.

        That is the point: ``app/`` has no idea it is running locally. It is
        given a workspace host that happens to be the launcher, and a Lakebase
        DSN that happens to be an embedded Postgres.
        """
        env = dict(os.environ)
        env.update(
            {
                # The one lie, and it is a URL: JobsApi talks to the launcher.
                "DATABRICKS_HOST": self.launcher_url,
                "DBX_JOB_IDS": json.dumps(self.job_ids),
                "DBX_APP_PUBLIC_URL": self.app_url,
                "DBX_APP_TOKEN": DEV_JOB_TOKEN,
                "DBX_LAKEBASE_DSN": self.dsn or "",
                "DBX_MAX_CONCURRENT_RUNS": str(self.max_concurrent_runs),
                "PYTHONUNBUFFERED": "1",
                "PYTHONPATH": str(REPO_ROOT),
            }
        )
        # No warehouse locally, and an inherited one would point the read path
        # at a real workspace from a stack that is otherwise entirely local.
        env.pop("DBX_WAREHOUSE_ID", None)
        return env

    def spawn(self, label: str, command: list[str], env: dict[str, str]) -> subprocess.Popen:
        process = subprocess.Popen(command, env=env, cwd=str(REPO_ROOT))  # noqa: S603
        self._children.append((label, process))
        self._spawned_at[label] = time.monotonic()
        return process

    def restart_app(self) -> bool:
        """Bring the app back after it exited, leaving the jobs running.

        The app going down while jobs keep running is the platform's normal
        state — apps get 24h, jobs do not share that schedule — so the dev loop
        has to survive it rather than treat it as the end of the session. Kill
        the app deliberately and you should see the running jobs carry on
        writing durably and then reattach; that is the property the whole
        transport design rests on, and it is worth being able to watch.

        Returns False if the app is failing fast enough that restarting it is
        just a crash loop with extra steps.
        """
        self._children = [(label, p) for label, p in self._children if label != "app"]
        lifetime = time.monotonic() - self._spawned_at.get("app", 0.0)
        self._app_restarts = self._app_restarts + 1 if lifetime < 10.0 else 1
        if self._app_restarts > 3:
            return False

        print(
            f"\napp exited after {lifetime:.1f}s — restarting it. Running jobs are "
            f"unaffected and will reattach within {self.ws_reconnect_s:.0f}s.",
            file=sys.stderr,
        )
        self.spawn("app", self.app_command(), self.app_env())
        try:
            self.wait_for_health(f"{self.app_url}/healthz", "app")
        except StackError as exc:
            print(f"  ...it did not come back: {exc}", file=sys.stderr)
            return False
        return True

    # --- waiting ----------------------------------------------------------

    def wait_for_health(self, url: str, label: str, timeout_s: float = 45.0) -> None:
        """Poll ``/healthz`` until it answers, or say plainly that it did not.

        Bounded, always: a dev script that hangs waiting for a process that
        already died is worse than one that fails.
        """
        import httpx

        deadline = time.monotonic() + timeout_s
        last: str = "no attempt made"
        while time.monotonic() < deadline:
            dead = self._dead_child(label)
            if dead is not None:
                raise StackError(
                    f"{label} exited with code {dead} before answering {url} — "
                    f"its output is above this message"
                )
            try:
                response = httpx.get(url, timeout=2.0)
                if response.status_code < 500:
                    return
                last = f"HTTP {response.status_code}"
            except Exception as exc:  # noqa: BLE001 - not up yet is the common case
                last = f"{type(exc).__name__}: {exc}"
            time.sleep(0.25)
        raise StackError(f"{label} did not answer {url} within {timeout_s:.0f}s ({last})")

    def _dead_child(self, label: str) -> int | None:
        for name, process in self._children:
            if name == label and process.poll() is not None:
                return process.returncode
        return None

    def poll(self) -> tuple[str, int] | None:
        """The first child that has exited, if any."""
        for label, process in self._children:
            code = process.poll()
            if code is not None:
                return label, code
        return None

    # --- teardown ---------------------------------------------------------

    def shutdown(self) -> None:
        # Launcher first: it takes the job processes with it, and a job that
        # outlives the app it was streaming to is a confusing thing to find.
        for label, process in reversed(self._children):
            if process.poll() is not None:
                continue
            print(f"  stopping {label}", file=sys.stderr)
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
        for _, process in reversed(self._children):
            with contextlib.suppress(Exception):
                process.wait(timeout=10)
            if process.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
        self._children.clear()
        if self._postgres is not None:
            print("  stopping postgres", file=sys.stderr)
            with contextlib.suppress(Exception):
                self._postgres.cleanup()
            self._postgres = None


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dev_stack",
        description="Run app + job launcher + registry locally, with no Databricks workspace.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Then: cd app/client && bun run dev",
    )
    parser.add_argument("--app-port", type=int, default=DEFAULT_APP_PORT)
    parser.add_argument("--launcher-port", type=int, default=DEFAULT_LAUNCHER_PORT)
    parser.add_argument(
        "--state-dir",
        type=pathlib.Path,
        default=None,
        help=f"default: {default_state_dir()}",
    )
    parser.add_argument(
        "--reset", action="store_true", help="wipe the state directory (registry and telemetry)"
    )
    parser.add_argument(
        "--models",
        default=None,
        help="comma-separated subset to expose as triggerable; default: every registered model",
    )
    parser.add_argument(
        "--max-concurrent-runs",
        type=int,
        default=5,
        help="the app's ceiling. Free Edition's real one is 5 account-wide; lower it to see "
        "the 429 sooner",
    )
    parser.add_argument(
        "--ws-reconnect-s",
        type=float,
        default=5.0,
        help="how often a job retries the WebSocket. Production default is 30s; 5s is "
        "friendlier when you keep restarting the app",
    )
    parser.add_argument("--reload", action="store_true", help="uvicorn --reload for app/")
    parser.add_argument("--quiet-jobs", action="store_true", help="do not echo job output here")
    return parser


def _banner(stack: DevStack) -> str:
    models = ", ".join(sorted(stack.job_ids)) or "(none)"
    fast = ", ".join(m for m in FAST_MODELS if m in stack.job_ids)
    return f"""
================================================================================
  dev stack up — no Databricks workspace involved
================================================================================
  app            {stack.app_url}          (real app/, real SSE, real WS ingress)
  job launcher   {stack.launcher_url}     (SUBSTITUTE for the Jobs API)
  registry       embedded Postgres        (real PostgresRunStore, real ceiling)
  telemetry      {stack.state_dir / "delta"}
                 local JSONL — NOT Unity Catalog
  job logs       {stack.state_dir / "job-logs"}

  triggerable    {models}
  start with     {fast or "(none of the fast models are enabled)"}

  Next:   cd app/client && bun run dev        (its proxy targets 127.0.0.1:8000)
  Or:     curl -sS -X POST {stack.app_url}/api/runs \\
            -H 'content-type: application/json' \\
            -d '{{"model":"bayesian_ab"}}'

  Degraded on purpose, because there is no SQL warehouse:
    GET /api/runs/{{id}}/messages  and  /api/runs/{{id}}/results  answer 503,
    and startup reconciliation is skipped. /healthz says so; live streaming,
    trigger, cancel and listing are unaffected.

  Ctrl-C stops the app, the launcher, every job it started, and Postgres.
================================================================================
"""


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    state_dir = args.state_dir or default_state_dir()
    models = [m.strip() for m in args.models.split(",") if m.strip()] if args.models else None
    if models:
        unknown = sorted(set(models) - set(model_names()))
        if unknown:
            print(
                f"error: unknown model(s) {unknown}; registered models are {model_names()}",
                file=sys.stderr,
            )
            return 2

    problems = preflight(
        ports={"app": args.app_port, "launcher": args.launcher_port},
    )
    if problems:
        print("cannot start the dev stack:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 2

    if args.reset and state_dir.exists():
        print(f"resetting {state_dir}", file=sys.stderr)
        shutil.rmtree(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)

    stack = DevStack(
        state_dir=state_dir,
        app_port=args.app_port,
        launcher_port=args.launcher_port,
        models=models,
        max_concurrent_runs=args.max_concurrent_runs,
        ws_reconnect_s=args.ws_reconnect_s,
        reload=args.reload,
        quiet_jobs=args.quiet_jobs,
    )

    try:
        print("starting embedded postgres (the Lakebase stand-in)...", file=sys.stderr)
        stack.start_postgres()
        stale = stack.reconcile_stale_runs()
        if stale:
            print(
                f"  failed {stale} run(s) left non-terminal by a previous session",
                file=sys.stderr,
            )

        print("starting the job launcher...", file=sys.stderr)
        stack.spawn("launcher", stack.launcher_command(), stack.launcher_env())
        stack.wait_for_health(f"{stack.launcher_url}/healthz", "launcher")

        print("starting the app...", file=sys.stderr)
        stack.spawn("app", stack.app_command(), stack.app_env())
        stack.wait_for_health(f"{stack.app_url}/healthz", "app")

        print(_banner(stack), file=sys.stderr)
        return _supervise(stack)
    except StackError as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        print("\nshutting down", file=sys.stderr)
        stack.shutdown()


def _supervise(stack: DevStack) -> int:
    """Sit until Ctrl-C, or until a child dies — whichever comes first."""

    # SIGTERM (a supervisor, a container stop) has to unwind the same way
    # Ctrl-C does, or the job processes outlive everything. SIGINT is set
    # explicitly rather than left to Python's default handler because a shell
    # that starts this in the background sets SIGINT to SIG_IGN, and CPython
    # keeps an inherited SIG_IGN — so `dev_stack &` would ignore Ctrl-C and
    # leak a Postgres, a launcher and every job it had started.
    def _stop(*_: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    while True:
        dead = stack.poll()
        if dead is None:
            time.sleep(0.3)
            continue
        label, code = dead
        # The app is the one child whose death is survivable — see restart_app.
        if label == "app" and stack.restart_app():
            continue
        print(f"\n{label} exited with code {code}; bringing the rest down", file=sys.stderr)
        return 1 if code else 0


if __name__ == "__main__":
    sys.exit(main())
