"""The supervisor that wires app, launcher and registry together locally.

Two things are worth testing here and the rest is plumbing:

- the environment it hands the app must be one ``app/config.py`` actually
  accepts, because the whole design depends on ``app/`` not knowing it is
  running locally;
- the preflight must name a fix for every prerequisite, because the failure
  mode this script exists to avoid is a developer staring at a hang.
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest

from app.config import AppConfig
from scripts import dev_stack
from scripts._registry import model_names
from scripts.dev_stack import DEV_JOB_TOKEN, DevStack, build_parser, preflight

REPO_ROOT = pathlib.Path(dev_stack.__file__).resolve().parent.parent


@pytest.fixture
def stack(tmp_path):
    return DevStack(state_dir=tmp_path, app_port=8123, launcher_port=8456)


# --- what the app is handed ------------------------------------------------


def test_the_app_environment_parses_as_a_real_app_config(stack):
    """Parsed by ``AppConfig.from_env`` itself, not eyeballed.

    If the stack ever hands the app something it cannot read, the failure
    should be here rather than as a 503 in a browser twenty seconds later.
    """
    config = AppConfig.from_env(stack.app_env())

    assert config.workspace_host == "http://127.0.0.1:8456"
    assert config.public_url == "http://127.0.0.1:8123"
    assert config.job_token == DEV_JOB_TOKEN
    assert config.triggerable_models == model_names()
    assert config.lakebase_dsn is not None or stack.dsn is None


def test_every_model_can_be_triggered_by_default(stack):
    assert sorted(json.loads(stack.app_env()["DBX_JOB_IDS"])) == model_names()


def test_a_subset_narrows_what_the_app_will_launch(tmp_path):
    narrowed = DevStack(state_dir=tmp_path, models=["scenario", "mcmc"])

    assert AppConfig.from_env(narrowed.app_env()).triggerable_models == ["mcmc", "scenario"]


def test_an_inherited_warehouse_is_dropped(stack, monkeypatch):
    """An entirely-local stack must not reach a real workspace's warehouse
    just because the shell that started it had the variable set."""
    monkeypatch.setenv("DBX_WAREHOUSE_ID", "abc123")

    assert AppConfig.from_env(stack.app_env()).warehouse_id is None


def test_the_job_ingress_token_reaches_both_sides(stack):
    """The app requires it on the WS/push ingress and hands it to the job as a
    parameter; a mismatch would show up as a job that silently never attaches."""
    assert stack.app_env()["DBX_APP_TOKEN"] == DEV_JOB_TOKEN
    assert "--app-token" in stack.launcher_command()
    assert DEV_JOB_TOKEN in stack.launcher_command()


def test_the_launcher_is_told_where_the_app_is(stack):
    """It needs this only to report a crashed job over the push ingress."""
    command = stack.launcher_command()

    assert command[command.index("--app-url") + 1] == stack.app_url


def test_the_job_reconnect_interval_reaches_the_spawned_job(tmp_path):
    stack = DevStack(state_dir=tmp_path, ws_reconnect_s=2.5)

    assert stack.launcher_env()["DBX_WS_RECONNECT_S"] == "2.5"


def test_the_app_is_served_by_uvicorn_on_the_configured_port(stack):
    command = stack.app_command()

    assert "app.main:app" in command
    assert command[command.index("--port") + 1] == "8123"
    assert "--reload" not in command


def test_reload_is_opt_in(tmp_path):
    assert "--reload" in DevStack(state_dir=tmp_path, reload=True).app_command()


# --- the frontend contract -------------------------------------------------


def test_the_default_port_is_the_one_vite_proxies_to():
    """``frontend/vite.config.ts`` hard-codes the dev proxy target.

    Two files have to agree on a port number and nothing else connects them,
    so this is the thing that notices when one of them moves.
    """
    config = REPO_ROOT / "frontend" / "vite.config.ts"
    if not config.exists():  # pragma: no cover - the frontend is optional here
        pytest.skip("no frontend checkout")

    targets = set(re.findall(r"(?:http|ws)://127\.0\.0\.1:(\d+)", config.read_text()))

    assert targets == {str(dev_stack.DEFAULT_APP_PORT)}, (
        "frontend/vite.config.ts proxies somewhere the dev stack does not serve; "
        "change DEFAULT_APP_PORT to match, or pass --app-port"
    )


# --- preflight -------------------------------------------------------------


def test_preflight_is_quiet_when_everything_is_present():
    assert preflight(ports={}, check_ports=False) == []


def test_a_missing_dependency_names_the_command_that_fixes_it(monkeypatch):
    import importlib.util

    real = importlib.util.find_spec

    def missing_pgserver(name, *args, **kwargs):
        return None if name == "pgserver" else real(name)

    monkeypatch.setattr(importlib.util, "find_spec", missing_pgserver)

    (problem,) = preflight(ports={}, check_ports=False)

    assert "pgserver" in problem
    assert "uv sync" in problem


def test_a_busy_port_is_reported_rather_than_waited_on():
    import socket

    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        held.listen()
        port = held.getsockname()[1]

        (problem,) = preflight(ports={"app": port})

    assert str(port) in problem
    assert "--app-port" in problem


def test_every_problem_is_collected_before_anything_starts(monkeypatch):
    """One list, not six consecutive runs each failing on the next thing."""
    import importlib.util

    monkeypatch.setattr(importlib.util, "find_spec", lambda *a, **k: None)

    problems = preflight(ports={}, check_ports=False)

    assert len(problems) == len(dev_stack.REQUIRED_IMPORTS)


# --- CLI -------------------------------------------------------------------


def test_an_unknown_model_is_refused_before_anything_is_started(capsys):
    assert dev_stack.main(["--models", "not_a_model"]) == 2

    assert "not_a_model" in capsys.readouterr().err


def test_the_ceiling_is_configurable_so_the_429_can_be_seen(tmp_path):
    """Free Edition's real ceiling is 5 account-wide. Locally there is no
    ceiling at all, so the app's own check is the only thing enforcing it —
    lowering it is how a developer sees the 429 without starting five runs."""
    args = build_parser().parse_args(["--max-concurrent-runs", "1"])
    stack = DevStack(state_dir=tmp_path, max_concurrent_runs=args.max_concurrent_runs)

    assert AppConfig.from_env(stack.app_env()).max_concurrent_runs == 1


def test_the_state_directory_is_outside_the_repository():
    """Seven agents share this checkout; a dev run must not add untracked
    files to it, and a stray Postgres data directory is a lot of them."""
    assert REPO_ROOT not in dev_stack.default_state_dir().parents


# --- the registry ----------------------------------------------------------


def test_stale_runs_from_a_previous_session_are_failed(tmp_path):
    """Otherwise five crashed runs from yesterday hold the whole ceiling.

    Locally this is knowable rather than guessed: the launcher owns every job
    process, so nothing from a previous session can still be running.
    """
    pgserver = pytest.importorskip("pgserver", reason="needs the dev group")
    import asyncio

    from app.store import PostgresRunStore
    from shared.envelope import RunStatus

    server = pgserver.get_server(tmp_path / "pg")
    try:
        stack = DevStack(state_dir=tmp_path)
        stack.dsn = server.get_uri()

        async def seed() -> None:
            store = PostgresRunStore(stack.dsn)
            await store.ensure_schema()
            await store.claim_slot("ghost", model="scenario", ceiling=5)
            await store.claim_slot("done", model="scenario", ceiling=5)
            await store.set_status("done", RunStatus.SUCCEEDED)

        asyncio.run(seed())

        assert stack.reconcile_stale_runs() == 1

        async def check() -> tuple[str, str, int]:
            store = PostgresRunStore(stack.dsn)
            ghost = await store.get("ghost")
            done = await store.get("done")
            return ghost.status.value, done.status.value, await store.active_count()

        ghost_status, done_status, active = asyncio.run(check())
        assert ghost_status == "FAILED"
        assert done_status == "SUCCEEDED", "a finished run must not be rewritten"
        assert active == 0, "the ceiling has to be free again"
    finally:
        server.cleanup()
