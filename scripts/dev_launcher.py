"""A stand-in for the Databricks Jobs API, so the platform can run with no workspace.

**This is the one substituted component in the local loop, and the only one.**
Everything else a local run touches is the real thing: the real ``job/``
harness in its own OS process, the real WebSocket ingress, the real relay, the
real envelope, the real seq counter, the real SSE stream. What is faked here is
strictly the two REST endpoints ``app/jobs_api.py`` calls — ``run-now`` and
``runs/get`` — because those are the only link in the chain that needs a
Databricks account.

Pointing ``DATABRICKS_HOST`` at this process is what makes that substitution
work without editing a line of ``app/``. The app builds its ``JobsApi`` from
that host exactly as it would in production; it simply reaches a launcher that
answers with a subprocess instead of a serverless task.

What this deliberately reproduces, because getting it wrong locally would hide
a real bug:

- **Parameters are rejected if the job has not declared them.** Databricks does
  this and it is why ``JOB_PARAMETER_NAMES`` is a tested contract; a launcher
  that accepted anything would let that contract rot.
- **Parameters travel as ``KEY=VALUE`` argv**, in the order the bundle declares
  them, empty values included — exactly what ``resources/*.job.yml`` does, so
  ``entrypoints/run_model.py`` is exercised rather than bypassed.
- **A cancel is a SIGTERM**, which is what ``databricks jobs cancel-run`` does
  and what ``job/main.py`` turns into a cooperative cancel.

What it cannot reproduce, and says so rather than pretending:

- **Per-model environments.** A real deploy gives each model its own serverless
  environment with only its own extra installed. Here every model runs in this
  one venv, so a model whose extra is missing fails at import instead of at
  deploy time.
- **The 5-concurrent-task account ceiling.** There is no ceiling on subprocesses.
  The app's own enforcement still runs and still answers 429 — that path is
  exercised, the platform's backstop behind it is not.
- **Serverless startup latency.** A subprocess is live in milliseconds; a real
  task takes tens of seconds. Anything that only breaks under that delay will
  not show up here.

One thing it does that Databricks does *not*: when a job process dies without
ever reporting a terminal status (a bad model config, a missing extra), the
launcher reports ``FAILED`` on its behalf over the real HTTP push ingress. In a
deploy, startup reconciliation reads that from the Jobs API — but
reconciliation needs the SQL warehouse read path, which does not exist locally,
so without this a crashed run would sit ``QUEUED`` forever holding a slot
against the concurrency ceiling.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import pathlib
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

# Module level, not inside create_launcher_app, and that is load-bearing:
# this file uses `from __future__ import annotations`, so FastAPI resolves a
# route's parameter annotations by name against the *module* globals. Imported
# inside the factory, `Request` is a local, the annotation does not resolve,
# and FastAPI quietly demotes the parameter to a required query string — a 422
# on every POST that looks nothing like the actual mistake.
from fastapi import FastAPI, HTTPException, Query, Request, status  # noqa: E402

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from scripts._registry import REPO_ROOT, model_names  # noqa: E402

log = logging.getLogger("dev.launcher")

__all__ = [
    "DECLARED_PARAMETERS",
    "LocalJobLauncher",
    "LocalRun",
    "UnknownJob",
    "UndeclaredParameter",
    "create_launcher_app",
    "dev_job_ids",
]

#: The parameter names, in the order ``resources/*.job.yml`` lists them on the
#: task. Sourced from the app's own contract rather than retyped, so a name
#: added there reaches the local loop without anyone remembering to.
def _declared_parameters() -> tuple[str, ...]:
    from app.routes.runs import JOB_PARAMETER_NAMES

    # DBX_RUN_ID and DBX_MODEL first because that is the order a human reads a
    # `ps` line in; the rest alphabetically, which is all the yml ordering is.
    lead = ("DBX_RUN_ID", "DBX_MODEL")
    rest = sorted(n for n in JOB_PARAMETER_NAMES if n not in lead)
    return (*lead, *rest)


DECLARED_PARAMETERS = _declared_parameters()

#: Model name -> a fake job id. Deterministic so the launcher and whatever
#: sets ``DBX_JOB_IDS`` cannot disagree, and offset far from 1 so a stray id
#: in a log is obviously not a real workspace's.
_JOB_ID_BASE = 900_000


def dev_job_ids(models: Iterable[str] | None = None) -> dict[str, int]:
    """The local ``DBX_JOB_IDS`` map: every registered model, triggerable."""
    names = sorted(models) if models is not None else model_names()
    return {name: _JOB_ID_BASE + i for i, name in enumerate(names)}


class UnknownJob(LookupError):
    """No job with that id — what Databricks answers 400 for."""


class UndeclaredParameter(ValueError):
    """A run-now parameter the job never declared. Databricks refuses these."""


@dataclass
class LocalRun:
    """One job run, which locally means one OS process."""

    job_run_id: int
    run_id: str
    model: str
    argv: list[str]
    log_path: pathlib.Path
    started_ts: float = field(default_factory=time.time)
    process: Any = None
    returncode: int | None = None
    finished_ts: float | None = None
    cancelled: bool = False
    #: Set once the launcher has reported a crash on the job's behalf, so the
    #: reaper does it exactly once.
    reported: bool = False

    @property
    def running(self) -> bool:
        return self.returncode is None

    def state(self) -> dict[str, Any]:
        """The subset of the Jobs API run shape ``app/jobs_api.py`` reads.

        Matching the real response *shape* matters more than matching all of
        it: ``JobsApi.terminal_status`` navigates ``status.state`` and
        ``status.termination_details.code``, and a launcher that invented its
        own shape would make that navigation untested locally.
        """
        if self.running:
            return {"state": "RUNNING"}
        if self.cancelled:
            code = "CANCELED"
        elif self.returncode == 0:
            code = "SUCCESS"
        else:
            code = "FAILED"
        return {"state": "TERMINATED", "termination_details": {"code": code}}

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.job_run_id,
            "platform_run_id": self.run_id,
            "model": self.model,
            "status": self.state(),
            "start_time": int(self.started_ts * 1000),
            "end_time": int(self.finished_ts * 1000) if self.finished_ts else 0,
            "returncode": self.returncode,
            "log": str(self.log_path),
        }


#: Signature of the process spawner, so tests can drive the launcher without
#: starting Python interpreters.
Spawn = Callable[[list[str], dict[str, str], pathlib.Path], Any]


def _spawn_process(argv: list[str], env: dict[str, str], log_path: pathlib.Path) -> Any:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = log_path.open("ab", buffering=0)
    try:
        return subprocess.Popen(  # noqa: S603 - argv is built here, never a shell string
            argv,
            env=env,
            stdout=handle,
            stderr=subprocess.STDOUT,
            cwd=str(REPO_ROOT),
            # Its own process group: a Ctrl-C in the terminal that started the
            # stack must not race the launcher's own orderly termination.
            start_new_session=True,
        )
    finally:
        handle.close()


class LocalJobLauncher:
    """Spawns and tracks the job processes a workspace would otherwise run."""

    def __init__(
        self,
        *,
        job_ids: Mapping[str, int] | None = None,
        state_dir: pathlib.Path | str = ".dev-stack",
        app_url: str | None = None,
        app_token: str | None = None,
        job_env: Mapping[str, str] | None = None,
        spawn: Spawn | None = None,
        python: str | None = None,
        tail_to: Any = None,
    ) -> None:
        self.job_ids = dict(job_ids or dev_job_ids())
        self.models_by_job_id = {v: k for k, v in self.job_ids.items()}
        self.state_dir = pathlib.Path(state_dir)
        self.app_url = app_url.rstrip("/") if app_url else None
        self.app_token = app_token
        self.job_env = dict(job_env or {})
        self._spawn = spawn or _spawn_process
        self.python = python or sys.executable
        #: Where a job's stdout is echoed, so one terminal shows everything.
        self.tail_to = tail_to
        self.runs: dict[int, LocalRun] = {}
        self._next_job_run_id = 1
        self._lock = threading.Lock()

    # --- the two endpoints app/jobs_api.py actually calls ------------------

    def run_now(self, job_id: int, parameters: Mapping[str, str]) -> int:
        model = self.models_by_job_id.get(int(job_id))
        if model is None:
            known = ", ".join(f"{name}={jid}" for name, jid in sorted(self.job_ids.items()))
            raise UnknownJob(
                f"no job {job_id}; the local launcher knows {known or '(nothing)'} — "
                f"the app and the launcher must be started from the same job map"
            )

        undeclared = sorted(set(parameters) - set(DECLARED_PARAMETERS))
        if undeclared:
            # Databricks refuses these, and that refusal is the reason
            # JOB_PARAMETER_NAMES exists. Reproducing it locally is the whole
            # point of not writing a permissive mock.
            raise UndeclaredParameter(
                f"job {job_id} ({model}) has not declared {undeclared}; declared "
                f"parameters are {list(DECLARED_PARAMETERS)}. Add them to "
                f"resources/model_{model}.job.yml and to JOB_PARAMETER_NAMES."
            )

        with self._lock:
            job_run_id = self._next_job_run_id
            self._next_job_run_id += 1

        run_id = parameters.get("DBX_RUN_ID") or f"local-{job_run_id}"
        argv = self.build_argv(parameters)
        log_path = self.state_dir / "job-logs" / f"{run_id}.log"
        run = LocalRun(
            job_run_id=job_run_id,
            run_id=run_id,
            model=model,
            argv=argv,
            log_path=log_path,
        )
        run.process = self._spawn(argv, self.build_env(job_run_id), log_path)
        self.runs[job_run_id] = run
        log.info(
            "run-now job %s (%s) -> local job run %s, pid %s, log %s",
            job_id,
            model,
            job_run_id,
            getattr(run.process, "pid", "?"),
            log_path,
        )
        self._start_tail(run)
        return job_run_id

    def get_run(self, job_run_id: int) -> LocalRun:
        run = self.runs.get(int(job_run_id))
        if run is None:
            raise UnknownJob(f"no local job run {job_run_id}")
        return run

    def cancel(self, job_run_id: int) -> LocalRun:
        """SIGTERM, which is what ``databricks jobs cancel-run`` sends.

        The documented escape hatch when no live channel exists, reproduced
        faithfully: ``job/main.py`` turns SIGTERM into a cooperative cancel, so
        the run keeps whatever results it had already produced.
        """
        run = self.get_run(job_run_id)
        if run.running and run.process is not None:
            run.cancelled = True
            try:
                run.process.terminate()
            except ProcessLookupError:  # pragma: no cover - already gone
                pass
        return run

    # --- what a job process is launched with ------------------------------

    def build_argv(self, parameters: Mapping[str, str]) -> list[str]:
        """``KEY=VALUE`` per declared parameter, in bundle order.

        Every declared name is passed even when empty, because that is what
        ``{{job.parameters.X}}`` expands to and ``parse_settings`` dropping an
        empty value is load-bearing: an unset ``DBX_APP_URL`` has to mean "no
        app", not "an app at ''".
        """
        entrypoint = REPO_ROOT / "entrypoints" / "run_model.py"
        return [
            self.python,
            str(entrypoint),
            *(f"{name}={parameters.get(name, '')}" for name in DECLARED_PARAMETERS),
        ]

    def build_env(self, job_run_id: int) -> dict[str, str]:
        env = dict(os.environ)
        env.update(self.job_env)
        # Databricks sets this; the harness records it for reconciliation.
        env["DATABRICKS_JOB_RUN_ID"] = str(job_run_id)
        env["PYTHONUNBUFFERED"] = "1"
        return env

    # --- lifecycle --------------------------------------------------------

    def reap(self) -> list[LocalRun]:
        """Collect finished processes. Returns the ones that just ended."""
        ended: list[LocalRun] = []
        for run in self.runs.values():
            if not run.running or run.process is None:
                continue
            code = run.process.poll()
            if code is None:
                continue
            run.returncode = code
            run.finished_ts = time.time()
            ended.append(run)
            log.info(
                "local job run %s (%s / %s) exited %s",
                run.job_run_id,
                run.model,
                run.run_id,
                code,
            )
        return ended

    def shutdown(self, grace_s: float = 5.0) -> None:
        """Terminate every job this launcher started.

        A job outliving the stack that launched it is the local equivalent of
        an orphaned cluster: it would keep writing telemetry nothing is
        listening to, and hold a slot in a registry nobody is reconciling.
        """
        alive = [r for r in self.runs.values() if r.running and r.process is not None]
        for run in alive:
            try:
                run.process.terminate()
            except ProcessLookupError:  # pragma: no cover
                pass
        deadline = time.time() + grace_s
        for run in alive:
            remaining = max(0.0, deadline - time.time())
            try:
                run.process.wait(timeout=remaining)
            except Exception:  # noqa: BLE001 - kill below covers every failure mode
                try:
                    run.process.kill()
                except ProcessLookupError:  # pragma: no cover
                    pass
        self.reap()

    # --- reporting a crash the app would otherwise never hear about -------

    async def report_orphan(self, run: LocalRun, client: Any) -> bool:
        """Tell the app about a job that died without a terminal status.

        Over ``POST /api/runs/{id}/push`` — the real one-way ingress, carrying
        a real envelope message — rather than by writing to the registry
        behind the app's back. The app's own ingest path then persists the
        status exactly as it would for a status the job had sent itself.

        Returns True if a message was pushed.
        """
        if self.app_url is None or run.reported:
            return False
        run.reported = True
        if run.returncode in (0, None) or run.cancelled:
            # A clean exit, or a cancel the harness turned into CANCELLED
            # itself. Nothing to invent.
            return False

        from shared.codec import to_jsonable
        from shared.envelope import RunStatus, StatusMessage, now_ms

        headers = {"Authorization": f"Bearer {self.app_token}"} if self.app_token else {}
        seq = 0
        try:
            resp = await client.get(f"{self.app_url}/api/runs/{run.run_id}", headers=headers)
            if resp.status_code == 200:
                last = resp.json().get("last_seq_seen")
                # Past whatever the job managed to send, so the SSE stream's
                # id ordering (and Last-Event-ID resume) still holds.
                seq = 0 if last is None else int(last) + 1
        except Exception:  # noqa: BLE001 - the app being down is a normal state
            log.debug("could not read last_seq_seen for %s", run.run_id, exc_info=True)

        detail = (
            f"the job process exited {run.returncode} without reporting a terminal "
            f"status; see {run.log_path}"
        )
        message = StatusMessage(
            run_id=run.run_id, seq=seq, ts=now_ms(), status=RunStatus.FAILED, detail=detail
        )
        try:
            await client.post(
                f"{self.app_url}/api/runs/{run.run_id}/push",
                json={"messages": [to_jsonable(message)]},
                headers=headers,
            )
        except Exception:  # noqa: BLE001
            log.warning("could not report the crash of %s to the app", run.run_id)
            return False
        log.warning("reported %s as FAILED on the job's behalf: %s", run.run_id, detail)
        return True

    # --- console tail -----------------------------------------------------

    def _start_tail(self, run: LocalRun) -> None:
        """Echo a job's log into the stack's own terminal, prefixed.

        One terminal showing app, launcher and every job is the difference
        between "watch the loop" and "go and find four log files".
        """
        stream = self.tail_to
        if stream is None:
            return

        def pump() -> None:
            prefix = f"[job {run.run_id}] "
            path = run.log_path
            deadline = time.time() + 10.0
            while not path.exists() and time.time() < deadline:
                time.sleep(0.05)
            if not path.exists():
                return
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                while True:
                    line = fh.readline()
                    if line:
                        stream.write(prefix + line)
                        stream.flush()
                        continue
                    if not run.running:
                        # One last read, then stop: the process may have
                        # flushed between the readline and the poll.
                        rest = fh.read()
                        for tail in rest.splitlines():
                            stream.write(prefix + tail + "\n")
                        stream.flush()
                        return
                    time.sleep(0.1)

        threading.Thread(target=pump, name=f"tail-{run.run_id}", daemon=True).start()


# --------------------------------------------------------------------------
# The HTTP surface: exactly the paths app/jobs_api.py builds, and no more.
# --------------------------------------------------------------------------


def create_launcher_app(launcher: LocalJobLauncher, *, reap_interval_s: float = 0.5) -> Any:
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app: Any):
        client = None
        if launcher.app_url:
            import httpx

            client = httpx.AsyncClient(timeout=10.0)

        async def reaper() -> None:
            while True:
                await asyncio.sleep(reap_interval_s)
                for run in launcher.reap():
                    if client is not None:
                        await launcher.report_orphan(run, client)

        task = asyncio.create_task(reaper(), name="launcher-reaper")
        try:
            yield
        finally:
            task.cancel()
            launcher.shutdown()
            if client is not None:
                await client.aclose()

    app = FastAPI(title="dev job launcher", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {
            "status": "ok",
            "kind": "local-job-launcher",
            "real": False,
            "jobs": launcher.job_ids,
            "active": sum(1 for r in launcher.runs.values() if r.running),
        }

    # The two POST bodies are read off the Request rather than declared as a
    # model. These routes exist to *mimic* a foreign API, so validating them
    # against a schema of our own invention would only make the mimicry
    # stricter than the thing it stands in for.
    @app.post("/api/2.2/jobs/run-now")
    async def run_now(request: Request) -> dict:
        body = await _json_object(request)
        job_id = body.get("job_id")
        parameters = body.get("job_parameters") or {}
        if job_id is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "job_id is required")
        try:
            job_run_id = launcher.run_now(int(job_id), parameters)
        except UnknownJob as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        except UndeclaredParameter as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        except OSError as exc:
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR, f"could not spawn the job: {exc}"
            ) from exc
        return {"run_id": job_run_id}

    @app.get("/api/2.2/jobs/runs/get")
    async def runs_get(run_id: str = Query(...)) -> dict:
        try:
            return launcher.get_run(int(run_id)).as_dict()
        except (UnknownJob, ValueError) as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    @app.post("/api/2.2/jobs/runs/cancel")
    async def runs_cancel(request: Request) -> dict:
        body = await _json_object(request)
        raw = body.get("run_id")
        if raw is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "run_id is required")
        try:
            run = launcher.cancel(int(raw))
        except (UnknownJob, TypeError, ValueError) as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        return {"run_id": run.job_run_id, "cancelled": True}

    @app.get("/_local/runs")
    async def local_runs() -> dict:
        """Not a Databricks endpoint. For a human looking at what is running."""
        return {"runs": [r.as_dict() for r in launcher.runs.values()]}

    async def _json_object(request: Request) -> dict:
        try:
            body = await request.json()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "expected a JSON body") from exc
        if not isinstance(body, dict):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "expected a JSON object")
        return body

    return app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--state-dir", default=".dev-stack")
    parser.add_argument("--app-url", default=os.environ.get("DBX_DEV_APP_URL") or None)
    parser.add_argument("--app-token", default=os.environ.get("DBX_APP_TOKEN") or None)
    parser.add_argument(
        "--quiet-jobs",
        action="store_true",
        help="do not echo job stdout into this terminal (it still goes to the log files)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")

    import uvicorn

    launcher = LocalJobLauncher(
        state_dir=args.state_dir,
        app_url=args.app_url,
        app_token=args.app_token,
        job_env=_job_env_from_environment(pathlib.Path(args.state_dir)),
        tail_to=None if args.quiet_jobs else sys.stderr,
    )

    # The stack supervisor sends SIGTERM; uvicorn turns that into a clean
    # shutdown, and lifespan's finally clause takes the job processes with it.
    def _bye(*_: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _bye)

    uvicorn.run(
        create_launcher_app(launcher),
        host=args.host,
        port=args.port,
        log_level="warning",
        access_log=False,
    )
    return 0


def _job_env_from_environment(state_dir: pathlib.Path) -> dict[str, str]:
    """What every spawned job gets on top of the launcher's own environment.

    The local durable writer is set here and nowhere else, so it is impossible
    to run the dev stack and think telemetry reached Unity Catalog: JSONL under
    the state directory, with ``DBX_ALLOW_LOCAL_WRITER`` opting in explicitly
    the way ``job/delta.py`` demands.
    """
    env = {
        "DBX_WRITER": "jsonl",
        "DBX_ALLOW_LOCAL_WRITER": "1",
        "DBX_LOCAL_ROOT": str(state_dir / "delta"),
    }
    # Pass-throughs a developer may want to override per stack run.
    for name in ("DBX_WS_RECONNECT_S", "DBX_FLUSH_MAX_AGE_S", "DBX_FLUSH_TICK_S"):
        value = os.environ.get(name)
        if value:
            env[name] = value
    return env


if __name__ == "__main__":
    sys.exit(main())
