"""The whole local loop, for real, with no Databricks and no mocks in the path.

What this drives, end to end:

    POST /api/runs  ->  PostgresRunStore.claim_slot  ->  the launcher
      ->  entrypoints/run_model.py in its own OS process  ->  job/ harness
      ->  a real model  ->  WebSocket (or HTTP push) into app/routes/ingest
      ->  hub.ingest  ->  SSE out of app/routes/stream

Only the Jobs API is substituted, and the durable writer is the local JSONL
one. Everything between the HTTP request and the browser's event stream is the
shipped code, in the arrangement it ships in.

Slower than the rest of the suite because it starts real processes. That is the
point: the bugs this catches are the ones that only exist between processes.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import pathlib
import socket
import threading

import pytest

from app.config import AppConfig
from app.main import create_app
from scripts.dev_launcher import LocalJobLauncher, create_launcher_app
from scripts.dev_stack import DEV_JOB_TOKEN, DevStack

pgserver = pytest.importorskip("pgserver", reason="needs the dev group")
pytest.importorskip("websockets", reason="needs the job extra")
pytest.importorskip("numpy", reason="bayesian_ab needs numpy")

pytestmark = pytest.mark.slow

#: The cheapest model there is. It also finishes in about half a second, which
#: makes it the hardest case for "can an observer attach at all" — the harness
#: does not wait for the WebSocket before starting the run, so this normally
#: goes out over the HTTP-push tier and then reconnects. Both tiers, one model.
MODEL = "bayesian_ab"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ThreadedServer:
    """A uvicorn server on a background thread.

    A thread rather than a subprocess only because this is a test: the app is
    the same ASGI application ``dev_stack.py`` serves, reached over a real
    socket, so the transport under test is unchanged.
    """

    def __init__(self, app, port: int) -> None:
        import uvicorn

        self._server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
        )
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self.port = port

    def __enter__(self) -> ThreadedServer:
        self._thread.start()
        deadline = threading.Event()
        for _ in range(400):  # 20s, then give up loudly rather than hang
            if self._server.started:
                return self
            deadline.wait(0.05)
        raise RuntimeError(f"uvicorn did not start on port {self.port}")

    def __exit__(self, *exc: object) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=20)


@pytest.fixture(scope="module")
def postgres(tmp_path_factory):
    server = pgserver.get_server(tmp_path_factory.mktemp("pg") / "data")
    try:
        yield server.get_uri()
    finally:
        server.cleanup()


@pytest.fixture
def stack(tmp_path, postgres):
    """The same wiring ``dev_stack.py`` builds, on throwaway ports."""
    built = DevStack(
        state_dir=tmp_path,
        app_port=free_port(),
        launcher_port=free_port(),
        models=[MODEL],
        ws_reconnect_s=1.0,
    )
    built.dsn = postgres
    built.reconcile_stale_runs()
    return built


@pytest.fixture
def running(stack):
    """App and launcher up, exactly as the stack starts them."""
    launcher = LocalJobLauncher(
        job_ids=stack.job_ids,
        state_dir=stack.state_dir,
        app_url=stack.app_url,
        app_token=DEV_JOB_TOKEN,
        job_env={
            "DBX_WRITER": "jsonl",
            "DBX_ALLOW_LOCAL_WRITER": "1",
            "DBX_LOCAL_ROOT": str(stack.state_dir / "delta"),
            "DBX_WS_RECONNECT_S": "1",
        },
    )
    app = create_app(AppConfig.from_env(stack.app_env()))
    with ThreadedServer(app, stack.app_port), ThreadedServer(
        create_launcher_app(launcher), stack.launcher_port
    ):
        yield stack, launcher
    launcher.shutdown()


async def wait_for_exit(launcher, *, timeout_s: float = 60.0) -> None:
    """Block until every spawned job process has exited."""
    for _ in range(int(timeout_s / 0.1)):
        launcher.reap()
        if all(not run.running for run in launcher.runs.values()):
            return
        await asyncio.sleep(0.1)
    pytest.fail(f"a job process was still running after {timeout_s}s")


async def read_stream(client, url: str, *, timeout_s: float = 90.0) -> list[dict]:
    """Consume SSE until a terminal status arrives, or fail saying what came.

    Bounded on purpose: a test that hangs waiting for a run that already died
    tells you nothing about why.
    """
    from shared.envelope import TERMINAL_STATUSES

    terminal = {s.value for s in TERMINAL_STATUSES}
    messages: list[dict] = []

    async def consume() -> None:
        async with client.stream("GET", url) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                message = json.loads(line[5:])
                messages.append(message)
                if message["type"] == "status" and message["status"] in terminal:
                    return

    try:
        await asyncio.wait_for(consume(), timeout=timeout_s)
    except TimeoutError:
        pytest.fail(f"no terminal status within {timeout_s}s; saw {len(messages)}: {messages[-3:]}")
    return messages


async def test_a_model_triggered_from_the_api_streams_back_over_sse(running):
    """The headline: one HTTP call in, real telemetry out, no workspace."""
    stack, _ = running
    import httpx

    async with httpx.AsyncClient(base_url=stack.app_url, timeout=30.0) as client:
        # Subscribe first, using a run_id we choose, so nothing is missed on a
        # model that finishes in half a second.
        run_id = "e2e-stream"
        stream = asyncio.create_task(
            read_stream(client, f"/api/runs/{run_id}/stream")
        )
        await asyncio.sleep(0.2)

        response = await client.post("/api/runs", json={"model": MODEL, "run_id": run_id})
        assert response.status_code == 202, response.text
        assert response.json()["run_id"] == run_id

        messages = await stream

    kinds = {m["type"] for m in messages}
    assert "status" in kinds
    assert "log" in kinds
    assert "result" in kinds, "the model's results must reach the browser"

    statuses = [m["status"] for m in messages if m["type"] == "status"]
    assert statuses[0] == "RUNNING"
    assert statuses[-1] == "SUCCEEDED"

    # One monotonic counter per run, assigned by the job — the property the
    # whole dedupe-live-against-backfill story rests on.
    seqs = [m["seq"] for m in messages]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)
    assert all(m["run_id"] == run_id for m in messages)


async def test_the_registry_records_the_run_the_job_actually_reported(running):
    stack, launcher = running
    import httpx

    async with httpx.AsyncClient(base_url=stack.app_url, timeout=30.0) as client:
        run_id = "e2e-registry"
        stream = asyncio.create_task(read_stream(client, f"/api/runs/{run_id}/stream"))
        await asyncio.sleep(0.2)
        await client.post("/api/runs", json={"model": MODEL, "run_id": run_id})
        await stream
        # The status write is off the ingest path deliberately (a cold
        # warehouse must not block a job's socket), so it lands a beat later.
        for _ in range(100):
            record = (await client.get(f"/api/runs/{run_id}")).json()["run"]
            if record["status"] == "SUCCEEDED":
                break
            await asyncio.sleep(0.1)

    assert record["status"] == "SUCCEEDED"
    assert record["model"] == MODEL
    # Databricks' own run id, which locally is the launcher's.
    assert record["job_run_id"] == str(next(iter(launcher.runs)))


async def test_the_durable_path_runs_regardless_and_lands_locally(running):
    """Delta is the floor, not a fallback tier — even when the live path worked.

    And it must be unmistakably local: JSONL under the state directory, never
    something that could be read as a Unity Catalog write.
    """
    stack, launcher = running
    import httpx

    async with httpx.AsyncClient(base_url=stack.app_url, timeout=30.0) as client:
        run_id = "e2e-durable"
        stream = asyncio.create_task(read_stream(client, f"/api/runs/{run_id}/stream"))
        await asyncio.sleep(0.2)
        await client.post("/api/runs", json={"model": MODEL, "run_id": run_id})
        await stream

    # The live path can beat the final flush to the finish — it is allowed to,
    # which is exactly why the terminal SSE event is not evidence that the
    # durable write happened. Wait for the process, not for the message.
    await wait_for_exit(launcher)

    delta = stack.state_dir / "delta"
    written = {path.name for path in delta.glob("*.jsonl")}
    assert "main.dbx_leaning.run_events.jsonl" in written
    assert f"main.dbx_leaning.results_{MODEL}.jsonl" in written

    events = [
        json.loads(line)
        for line in (delta / "main.dbx_leaning.run_events.jsonl").read_text().splitlines()
    ]
    assert {e["status"] for e in events if e["run_id"] == run_id} >= {"RUNNING", "SUCCEEDED"}


async def test_the_concurrency_ceiling_answers_429_rather_than_launching(running):
    """Free Edition allows 5 concurrent job tasks account-wide. Locally there
    is no such limit, so the app's own check is the only thing enforcing it —
    which makes it worth being able to watch it refuse."""
    stack, launcher = running
    import httpx

    stack_config = AppConfig.from_env(stack.app_env())
    assert stack_config.max_concurrent_runs == 5

    async with httpx.AsyncClient(base_url=stack.app_url, timeout=30.0) as client:
        # Occupy every slot with runs that are registered but never launched,
        # so the test does not depend on five real processes staying alive.
        from app.store import PostgresRunStore

        store = PostgresRunStore(stack.dsn or "")
        for i in range(5):
            await store.claim_slot(f"hog-{i}", model=MODEL, ceiling=5)

        response = await client.post("/api/runs", json={"model": MODEL, "run_id": "denied"})

    assert response.status_code == 429
    assert "ceiling" in response.json()["detail"]
    assert launcher.runs == {}, "nothing may be launched once the ceiling is reached"


async def test_a_run_that_starts_while_the_app_is_down_still_completes(stack, tmp_path):
    """The property the whole architecture rests on, and the one the local loop
    would be worthless without: no app at all, and the run is still durable.

    Nothing is listening here — no app process exists — so the job's live
    channel fails on every attempt and the run has to be unaffected by it.
    """
    launcher = LocalJobLauncher(
        job_ids=stack.job_ids,
        state_dir=stack.state_dir,
        app_url=None,
        job_env={
            "DBX_WRITER": "jsonl",
            "DBX_ALLOW_LOCAL_WRITER": "1",
            "DBX_LOCAL_ROOT": str(stack.state_dir / "delta"),
        },
    )
    job_run_id = launcher.run_now(
        stack.job_ids[MODEL],
        {
            "DBX_RUN_ID": "unobserved",
            "DBX_MODEL": f"models.{MODEL}",
            "DBX_MODEL_CONFIG": "{}",
            # Pointing at nothing on purpose: an app that is down, not absent.
            "DBX_APP_URL": f"http://127.0.0.1:{free_port()}",
        },
    )

    run = launcher.get_run(job_run_id)
    for _ in range(600):
        launcher.reap()
        if not run.running:
            break
        await asyncio.sleep(0.1)

    # Read off the loop: ASYNC240, and the log can be large on a failure.
    log_text = await asyncio.to_thread(pathlib.Path(run.log_path).read_text)
    assert run.returncode == 0, log_text

    raw_events = await asyncio.to_thread(
        (stack.state_dir / "delta" / "main.dbx_leaning.run_events.jsonl").read_text
    )
    events = [json.loads(line) for line in raw_events.splitlines()]
    assert {e["status"] for e in events if e["run_id"] == "unobserved"} >= {
        "RUNNING",
        "SUCCEEDED",
    }
    with contextlib.suppress(Exception):
        launcher.shutdown(grace_s=1.0)
