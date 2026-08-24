"""The local stand-in for the Jobs API.

The tests that matter here are the ones about *fidelity*: the launcher exists
so a developer exercises the real trigger path, and a launcher that quietly
accepted something Databricks refuses (or produced a response shape
``app/jobs_api.py`` does not actually read) would hide exactly the bugs the
local loop is meant to surface.
"""

from __future__ import annotations

import json
import pathlib

import pytest
from fastapi.testclient import TestClient

from app.jobs_api import JobsApi
from app.routes.runs import JOB_PARAMETER_NAMES, build_job_parameters
from entrypoints.run_model import parse_settings
from scripts._registry import model_names
from scripts.dev_launcher import (
    DECLARED_PARAMETERS,
    LocalJobLauncher,
    LocalRun,
    UndeclaredParameter,
    UnknownJob,
    create_launcher_app,
    dev_job_ids,
)
from shared.envelope import MessageAdapter, RunStatus, StatusMessage


class FakeProcess:
    """A process that does nothing, so a test never starts an interpreter."""

    def __init__(self) -> None:
        self.pid = 4242
        self.exit_code: int | None = None
        self.terminated = False

    def poll(self) -> int | None:
        return self.exit_code

    def terminate(self) -> None:
        self.terminated = True
        if self.exit_code is None:
            self.exit_code = -15

    def wait(self, timeout: float | None = None) -> int:
        return self.exit_code or 0

    def kill(self) -> None:
        self.exit_code = -9


@pytest.fixture
def launcher(tmp_path):
    spawned: list[tuple[list[str], dict[str, str], pathlib.Path]] = []

    def spawn(argv, env, log_path):
        spawned.append((argv, env, log_path))
        return FakeProcess()

    made = LocalJobLauncher(state_dir=tmp_path, spawn=spawn, python="/usr/bin/python3")
    made.spawned = spawned  # type: ignore[attr-defined]
    return made


def _parameters(**overrides) -> dict[str, str]:
    base = {"DBX_RUN_ID": "r1", "DBX_MODEL": "models.scenario", "DBX_MODEL_CONFIG": "{}"}
    base.update(overrides)
    return base


# --- the job map ----------------------------------------------------------


def test_every_registered_model_is_triggerable_locally():
    """A model with no entry here cannot be clicked in the browser at all —
    ``GET /api/models`` is derived from this map."""
    ids = dev_job_ids()

    assert sorted(ids) == model_names()
    assert len(set(ids.values())) == len(ids), "job ids must be unique"


def test_the_job_map_is_stable_across_calls():
    # The launcher and whatever sets DBX_JOB_IDS derive it separately; if this
    # were not deterministic, the app would trigger job ids the launcher had
    # never heard of.
    assert dev_job_ids() == dev_job_ids()


def test_a_subset_can_be_exposed():
    assert sorted(dev_job_ids(["mcmc", "scenario"])) == ["mcmc", "scenario"]


# --- the parameter contract -----------------------------------------------


def test_declared_parameters_are_exactly_the_apps_contract():
    """``JOB_PARAMETER_NAMES`` is the pinned contract; this must not drift."""
    assert set(DECLARED_PARAMETERS) == set(JOB_PARAMETER_NAMES)


def test_the_apps_own_trigger_parameters_are_accepted(launcher):
    """The real ``build_job_parameters`` output, through the real launcher.

    Neither side stubbed: if the app grows a parameter and the bundle does not,
    this fails here as well as in tests/deploy.
    """

    class Config:
        catalog = "main"
        schema = "dbx_leaning"
        public_url = "http://127.0.0.1:8000"
        job_token = "tok"

    parameters = build_job_parameters("r1", "scenario", {"seed": 7}, Config())

    launcher.run_now(launcher.job_ids["scenario"], parameters)

    argv, _, _ = launcher.spawned[0]
    assert parse_settings(argv[2:])["DBX_MODEL"] == "models.scenario"


def test_an_undeclared_parameter_is_refused_the_way_databricks_refuses_it(launcher):
    with pytest.raises(UndeclaredParameter) as excinfo:
        launcher.run_now(launcher.job_ids["scenario"], _parameters(DBX_SECRET_SAUCE="1"))

    message = str(excinfo.value)
    assert "DBX_SECRET_SAUCE" in message
    # The message has to name both places that must change, or the next person
    # fixes one of them and gets the same failure from the other.
    assert "resources/model_scenario.job.yml" in message
    assert "JOB_PARAMETER_NAMES" in message
    assert launcher.spawned == [], "nothing may be launched on a rejected trigger"


def test_an_unknown_job_id_says_what_it_does_know(launcher):
    with pytest.raises(UnknownJob) as excinfo:
        launcher.run_now(1234, _parameters())

    assert "scenario" in str(excinfo.value)


def test_argv_matches_what_a_serverless_task_passes(launcher):
    """``KEY=VALUE`` for every declared name, empty ones included.

    Empty values are the point: ``{{job.parameters.DBX_APP_URL}}`` expands to
    an empty string when unset, and ``parse_settings`` dropping it is what
    makes "no app" different from "an app at ''".
    """
    launcher.run_now(launcher.job_ids["scenario"], _parameters())

    argv, _, _ = launcher.spawned[0]
    assert argv[0] == "/usr/bin/python3"
    assert argv[1].endswith("entrypoints/run_model.py")

    arguments = argv[2:]
    assert [a.partition("=")[0] for a in arguments] == list(DECLARED_PARAMETERS)
    assert "DBX_APP_URL=" in arguments

    settings = parse_settings(arguments)
    assert settings == _parameters()
    assert "DBX_APP_URL" not in settings


def test_the_spawned_job_writes_locally_and_says_so(tmp_path):
    launcher = LocalJobLauncher(
        state_dir=tmp_path,
        spawn=lambda *a: FakeProcess(),
        job_env={
            "DBX_WRITER": "jsonl",
            "DBX_ALLOW_LOCAL_WRITER": "1",
            "DBX_LOCAL_ROOT": str(tmp_path / "delta"),
        },
    )
    env = launcher.build_env(job_run_id=17)

    # job/delta.py refuses a local writer without the explicit opt-in, which is
    # the whole reason a local run cannot be mistaken for a Unity Catalog one.
    assert env["DBX_WRITER"] == "jsonl"
    assert env["DBX_ALLOW_LOCAL_WRITER"] == "1"
    assert env["DATABRICKS_JOB_RUN_ID"] == "17"


# --- the response shape app/jobs_api.py actually reads ---------------------


@pytest.mark.parametrize(
    ("returncode", "cancelled", "expected"),
    [
        (None, False, None),  # still running
        (0, False, "SUCCEEDED"),
        (1, False, "FAILED"),
        (-15, True, "CANCELLED"),
    ],
)
def test_run_state_is_read_correctly_by_the_real_jobs_api_parser(
    returncode, cancelled, expected, tmp_path
):
    """Asserted through ``JobsApi.terminal_status``, not against a literal.

    That method navigates ``status.state`` and ``status.termination_details``;
    a launcher inventing its own shape would leave that navigation untested
    everywhere it matters.
    """
    run = LocalRun(
        job_run_id=1,
        run_id="r1",
        model="scenario",
        argv=[],
        log_path=tmp_path / "r1.log",
        returncode=returncode,
        cancelled=cancelled,
    )

    assert JobsApi.terminal_status(run.as_dict()) == expected


def test_cancel_is_a_sigterm(launcher):
    """The same signal ``databricks jobs cancel-run`` sends, so ``job/main.py``
    turns it into a cooperative cancel that keeps partial results."""
    job_run_id = launcher.run_now(launcher.job_ids["scenario"], _parameters())

    run = launcher.cancel(job_run_id)

    assert run.process.terminated
    assert run.cancelled


def test_shutdown_takes_every_job_with_it(launcher):
    launcher.run_now(launcher.job_ids["scenario"], _parameters(DBX_RUN_ID="a"))
    launcher.run_now(launcher.job_ids["mcmc"], _parameters(DBX_RUN_ID="b"))

    launcher.shutdown(grace_s=0.1)

    assert all(run.process.terminated for run in launcher.runs.values())


# --- crash reporting -------------------------------------------------------


class RecordingClient:
    """Just enough httpx to see what the launcher would have sent."""

    def __init__(self, last_seq: int | None = 11) -> None:
        self.last_seq = last_seq
        self.posted: list[dict] = []

    async def get(self, url, headers=None):
        class Response:
            status_code = 200

            @staticmethod
            def json():
                return {"last_seq_seen": self.last_seq}

        return Response()

    async def post(self, url, json=None, headers=None):
        self.posted.append({"url": url, "body": json, "headers": headers})

        class Response:
            status_code = 202

        return Response()


@pytest.fixture
def crashing(tmp_path):
    launcher = LocalJobLauncher(
        state_dir=tmp_path,
        spawn=lambda *a: FakeProcess(),
        app_url="http://app.test",
        app_token="tok",
    )
    job_run_id = launcher.run_now(launcher.job_ids["scenario"], _parameters())
    return launcher, launcher.get_run(job_run_id)


async def test_a_job_that_died_without_a_status_is_reported_as_failed(crashing):
    """Otherwise the run sits QUEUED forever, holding a concurrency slot.

    A deploy learns this from the Jobs API at startup; there is no startup
    reconciliation locally because it needs the warehouse read path.
    """
    launcher, run = crashing
    run.process.exit_code = 1
    launcher.reap()
    client = RecordingClient(last_seq=11)

    assert await launcher.report_orphan(run, client) is True

    (post,) = client.posted
    assert post["url"] == "http://app.test/api/runs/r1/push"
    assert post["headers"] == {"Authorization": "Bearer tok"}
    # It goes in as a real envelope over the real ingress, not as a poke at
    # the registry behind the app's back.
    (raw,) = post["body"]["messages"]
    message = MessageAdapter.validate_python(raw)
    assert isinstance(message, StatusMessage)
    assert message.status is RunStatus.FAILED
    assert message.run_id == "r1"
    # Past everything the job managed to send, so SSE ids stay ordered.
    assert message.seq == 12
    assert "exited 1" in (message.detail or "")


async def test_a_crash_is_reported_once(crashing):
    launcher, run = crashing
    run.process.exit_code = 1
    launcher.reap()
    client = RecordingClient()

    await launcher.report_orphan(run, client)
    await launcher.report_orphan(run, client)

    assert len(client.posted) == 1


async def test_nothing_is_invented_for_a_clean_exit(crashing):
    launcher, run = crashing
    run.process.exit_code = 0
    launcher.reap()
    client = RecordingClient()

    assert await launcher.report_orphan(run, client) is False
    assert client.posted == []


async def test_nothing_is_invented_for_a_cancel(crashing):
    """The harness turns SIGTERM into its own CANCELLED status. Overwriting
    that with FAILED would lose the distinction the whole cancel path exists
    to preserve."""
    launcher, run = crashing
    launcher.cancel(run.job_run_id)
    launcher.reap()
    client = RecordingClient()

    assert await launcher.report_orphan(run, client) is False
    assert client.posted == []


async def test_a_crash_with_no_app_listening_is_not_an_error(tmp_path):
    launcher = LocalJobLauncher(state_dir=tmp_path, spawn=lambda *a: FakeProcess())
    job_run_id = launcher.run_now(launcher.job_ids["scenario"], _parameters())
    run = launcher.get_run(job_run_id)
    run.process.exit_code = 1
    launcher.reap()

    assert await launcher.report_orphan(run, client=None) is False


# --- the HTTP surface ------------------------------------------------------


@pytest.fixture
def client(launcher):
    # reap_interval kept long: these tests drive the launcher directly, and a
    # background reaper racing them would make failures look intermittent.
    with TestClient(create_launcher_app(launcher, reap_interval_s=3600)) as test_client:
        yield test_client


def test_run_now_answers_the_shape_jobs_api_parses(client, launcher):
    response = client.post(
        "/api/2.2/jobs/run-now",
        json={"job_id": launcher.job_ids["scenario"], "job_parameters": _parameters()},
    )

    assert response.status_code == 200
    assert isinstance(response.json()["run_id"], int)


def test_run_now_refuses_an_unknown_job(client):
    response = client.post("/api/2.2/jobs/run-now", json={"job_id": 7, "job_parameters": {}})

    assert response.status_code == 400


def test_run_now_refuses_an_undeclared_parameter(client, launcher):
    response = client.post(
        "/api/2.2/jobs/run-now",
        json={
            "job_id": launcher.job_ids["scenario"],
            "job_parameters": _parameters(NOPE="1"),
        },
    )

    assert response.status_code == 400
    assert "NOPE" in response.json()["detail"]


def test_runs_get_reports_a_running_job(client, launcher):
    job_run_id = client.post(
        "/api/2.2/jobs/run-now",
        json={"job_id": launcher.job_ids["scenario"], "job_parameters": _parameters()},
    ).json()["run_id"]

    payload = client.get("/api/2.2/jobs/runs/get", params={"run_id": job_run_id}).json()

    assert JobsApi.terminal_status(payload) is None


def test_runs_cancel_terminates_the_process(client, launcher):
    job_run_id = client.post(
        "/api/2.2/jobs/run-now",
        json={"job_id": launcher.job_ids["scenario"], "job_parameters": _parameters()},
    ).json()["run_id"]

    assert client.post("/api/2.2/jobs/runs/cancel", json={"run_id": job_run_id}).status_code == 200
    assert launcher.get_run(job_run_id).process.terminated


def test_healthz_does_not_claim_to_be_databricks(client):
    body = client.get("/healthz").json()

    assert body["real"] is False
    assert body["kind"] == "local-job-launcher"


def test_a_non_object_body_is_a_400_not_a_traceback(client):
    assert client.post("/api/2.2/jobs/run-now", content=b"[]").status_code == 400
    assert client.post("/api/2.2/jobs/run-now", content=b"not json").status_code == 400


def test_the_job_map_is_visible_for_debugging(client):
    assert json.loads(client.get("/healthz").text)["jobs"]
