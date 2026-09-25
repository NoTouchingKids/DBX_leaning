"""Finding the platform's jobs when the environment did not name them.

`DBX_JOB_IDS` reaches the app only when the LIVE app deployment was created by
the bundle. A deploy that skipped `bundle run`, or a redeploy from the Apps UI,
leaves the app running an environment built from `app/app.yaml` — which cannot
carry job ids, because a hand deploy has no bundle to interpolate them from.

The symptom was an app that worked in every respect except that

    GET /api/models  ->  {"models": [], "default_job_id": null}

with nothing on the app to say why. These tests cover the fallback and, just as
importantly, that the empty answer now explains itself.
"""

from __future__ import annotations

import asyncio

import pytest

from server.config import AppConfig
from server.discovery import map_jobs_to_models
from server.services import ServiceHub


def job(job_id: int, name: str, tags: dict | None = None) -> dict:
    settings: dict = {"name": name}
    if tags is not None:
        settings["tags"] = tags
    return {"job_id": job_id, "settings": settings}


def tagged(job_id: int, model: str, name: str = "whatever") -> dict:
    return job(job_id, name, {"project": "dbx-leaning", "model": model})


class TestMatching:
    def test_a_tagged_job_is_matched_by_its_tag(self):
        found = map_jobs_to_models([tagged(11, "scenario")])
        assert found.job_ids == {"scenario": 11}

    def test_an_untagged_job_is_matched_by_its_name(self):
        """`expand_tasks=false` trims `settings`, and what survives has varied,
        so the name is the half that is always there."""
        found = map_jobs_to_models([job(12, "[dev] dbx-leaning · mcmc")])
        assert found.job_ids == {"mcmc": 12}

    def test_the_development_prefix_does_not_defeat_the_name_match(self):
        """`mode: development` prepends `[dev <user>] ` to a name that already
        starts `[dev] `, so anchoring at the front would match nothing."""
        found = map_jobs_to_models([job(13, "[dev kp25179] [dev] dbx-leaning · panel_fit")])
        assert found.job_ids == {"panel_fit": 13}

    @pytest.mark.parametrize(
        "name",
        [
            "dbx-leaning nightly refresh",  # the project name, not one of ours
            "some other team's job",
            "dbx-leaning · scenario extras",  # the model must end the name
            "",
        ],
    )
    def test_a_job_that_is_not_ours_is_left_alone(self, name):
        assert map_jobs_to_models([job(99, name)]).job_ids == {}

    def test_a_tag_from_another_project_does_not_count(self):
        other = job(99, "x", {"project": "something-else", "model": "scenario"})
        assert map_jobs_to_models([other]).job_ids == {}

    def test_a_job_with_an_unusable_id_is_skipped_rather_than_crashing(self):
        broken = {"settings": {"name": "[dev] dbx-leaning · mcmc"}}
        assert map_jobs_to_models([broken, tagged(7, "scenario")]).job_ids == {"scenario": 7}


class TestAmbiguity:
    """Deploying `dev` and `prod` into one workspace makes two jobs per model,
    and nothing visible from outside says which is which."""

    def test_the_highest_id_wins_and_the_loser_is_reported(self):
        found = map_jobs_to_models([tagged(4, "scenario"), tagged(9, "scenario")])
        assert found.job_ids == {"scenario": 9}
        assert found.ambiguous == {"scenario": [4, 9]}

    def test_an_unambiguous_match_reports_nothing(self):
        assert map_jobs_to_models([tagged(4, "scenario")]).ambiguous == {}


class FakeJobsApi:
    available = True

    def __init__(self, jobs=None, error: Exception | None = None) -> None:
        #: Both reassignable mid-test: the workspace changing under a running
        #: app is exactly what the periodic refresh is for.
        self.jobs = jobs or []
        self.error = error
        self.calls = 0
        self.called = asyncio.Event()

    async def list_jobs(self, **_):
        self.calls += 1
        self.called.set()
        if self.error is not None:
            raise self.error
        return self.jobs

    async def close(self): ...


async def resolve(config: AppConfig, jobs_api) -> ServiceHub:
    hub = ServiceHub(config)
    hub.jobs_api = jobs_api
    await hub.refresh_job_ids()
    return hub


class TestStartup:
    async def test_an_explicit_map_wins_and_nothing_is_looked_up(self):
        """DBX_JOB_IDS is the allow-list as well as the map. A deployment that
        names three models means three, not "and whatever else is around"."""
        api = FakeJobsApi([tagged(1, "mcmc"), tagged(2, "scenario")])
        hub = await resolve(AppConfig(job_ids={"scenario": 500}), api)

        assert hub.config.job_ids == {"scenario": 500}
        assert hub.job_ids_source == "config"
        assert api.calls == 0

    async def test_a_default_job_also_counts_as_configured(self):
        api = FakeJobsApi([tagged(1, "mcmc")])
        hub = await resolve(AppConfig(default_job_id=77), api)

        assert hub.job_ids_source == "config"
        assert api.calls == 0

    async def test_an_absent_map_is_discovered_from_the_workspace(self):
        api = FakeJobsApi([tagged(1, "mcmc"), job(2, "[dev] dbx-leaning · scenario")])
        hub = await resolve(AppConfig(), api)

        assert hub.config.job_ids == {"mcmc": 1, "scenario": 2}
        assert hub.job_ids_source == "discovered"
        assert "job_ids" not in hub.degraded

    async def test_discovery_does_not_mutate_the_frozen_config_in_place(self):
        config = AppConfig()
        hub = await resolve(config, FakeJobsApi([tagged(1, "mcmc")]))

        assert config.job_ids == {}, "the original must be left alone"
        assert hub.config is not config

    async def test_no_jobs_api_is_degraded_not_fatal(self):
        hub = ServiceHub(AppConfig())
        hub.jobs_api = None
        await hub.refresh_job_ids()

        assert hub.job_ids_source == "none"
        assert "no Jobs API" in hub.degraded["job_ids"]

    async def test_a_failing_lookup_is_degraded_not_fatal(self):
        """A 403 here is entirely plausible — the app's principal may have no
        Jobs access at all — and it must not stop the app from starting, since
        observing runs someone else triggered still works."""
        api = FakeJobsApi(error=RuntimeError("HTTP 403 PERMISSION_DENIED"))
        hub = await resolve(AppConfig(), api)

        assert hub.job_ids_source == "none"
        assert "403" in hub.degraded["job_ids"]

    async def test_finding_none_of_ours_says_how_many_it_looked_at(self):
        """The difference between "the app cannot see any jobs" and "the app
        sees plenty, none of them ours" is the whole diagnosis."""
        api = FakeJobsApi([job(1, "someone else's job"), job(2, "another")])
        hub = await resolve(AppConfig(), api)

        assert hub.job_ids_source == "none"
        assert "2 jobs" in hub.degraded["job_ids"]

    async def test_ambiguity_is_reported_without_blocking_triggering(self):
        api = FakeJobsApi([tagged(4, "scenario"), tagged(9, "scenario")])
        hub = await resolve(AppConfig(), api)

        assert hub.config.job_ids == {"scenario": 9}
        assert "job_ids_ambiguous" in hub.degraded
        assert "job_ids" not in hub.degraded, "ambiguous is not the same as absent"


async def eventually(api: FakeJobsApi, predicate) -> None:
    """Wait for the background refresh to have done something, without a
    fixed sleep that is either flaky or slow.

    Woken by each `list_jobs` call. The refresh does not yield between that
    call returning and the map being updated, so by the time this waiter
    runs the hub already reflects the answer.
    """
    async with asyncio.timeout(2.0):
        while not predicate():
            api.called.clear()
            await api.called.wait()


class TestProjectTag:
    """`DBX_PROJECT_TAG` scopes discovery per deployment, so a team running
    its own instance finds its own jobs without forking discovery.py."""

    def test_the_configured_tag_is_what_filters(self):
        ours = job(1, "x", {"project": "team-x", "model": "mcmc"})
        theirs = tagged(2, "scenario")  # project: dbx-leaning
        found = map_jobs_to_models([ours, theirs], project_tag="team-x")
        assert found.job_ids == {"mcmc": 1}

    def test_the_default_tag_is_still_dbx_leaning(self):
        ours = job(1, "x", {"project": "team-x", "model": "mcmc"})
        assert map_jobs_to_models([ours, tagged(2, "scenario")]).job_ids == {"scenario": 2}

    def test_the_name_fallback_follows_the_tag(self):
        """Otherwise a `team-x` deployment would pick up this project's jobs by
        name whenever a list response happened to omit tags."""
        jobs = [job(1, "[dev] team-x · mcmc"), job(2, "[dev] dbx-leaning · scenario")]
        assert map_jobs_to_models(jobs, project_tag="team-x").job_ids == {"mcmc": 1}

    def test_a_tag_is_matched_literally_not_as_a_pattern(self):
        assert map_jobs_to_models([job(1, "teamAx · mcmc")], project_tag="team.x").job_ids == {}

    async def test_the_hub_discovers_with_the_configured_tag(self):
        api = FakeJobsApi([tagged(1, "scenario"), job(2, "y", {"project": "t", "model": "mcmc"})])
        hub = await resolve(AppConfig(project_tag="t"), api)
        assert hub.config.job_ids == {"mcmc": 2}

    async def test_finding_none_names_the_tag_it_looked_for(self):
        hub = await resolve(AppConfig(project_tag="team-x"), FakeJobsApi([tagged(1, "mcmc")]))
        assert "project=team-x" in hub.degraded["job_ids"]

    @pytest.mark.parametrize(
        ("env", "expected"),
        [
            ({}, "dbx-leaning"),
            ({"DBX_PROJECT_TAG": "team-x"}, "team-x"),
            ({"DBX_PROJECT_TAG": "  "}, "dbx-leaning"),
        ],
    )
    def test_the_tag_is_read_from_the_environment(self, env, expected):
        assert AppConfig.from_env(env).project_tag == expected

    def test_healthz_reports_the_tag_in_use(self, app_and_hub):
        from fastapi.testclient import TestClient

        app, _ = app_and_hub(AppConfig(project_tag="team-x"))
        body = TestClient(app).get("/api/healthz").json()
        assert body["discovery"]["project_tag"] == "team-x"


class TestRefresh:
    """What one refresh does to a map an earlier one found."""

    async def test_a_refresh_picks_up_a_new_job(self):
        api = FakeJobsApi([tagged(1, "mcmc")])
        hub = await resolve(AppConfig(), api)

        api.jobs = [tagged(1, "mcmc"), tagged(2, "scenario")]
        assert await hub.refresh_job_ids() is True
        assert hub.config.job_ids == {"mcmc": 1, "scenario": 2}

    async def test_a_recreated_job_s_new_id_replaces_the_old(self):
        api = FakeJobsApi([tagged(1, "mcmc")])
        hub = await resolve(AppConfig(), api)

        api.jobs = [tagged(5, "mcmc")]
        await hub.refresh_job_ids()
        assert hub.config.job_ids == {"mcmc": 5}

    async def test_a_job_gone_from_a_non_empty_answer_is_dropped(self):
        api = FakeJobsApi([tagged(1, "mcmc"), tagged(2, "scenario")])
        hub = await resolve(AppConfig(), api)

        api.jobs = [tagged(1, "mcmc")]
        await hub.refresh_job_ids()
        assert hub.config.job_ids == {"mcmc": 1}

    async def test_a_failed_refresh_keeps_the_last_good_map_and_reports_it(self):
        api = FakeJobsApi([tagged(1, "mcmc")])
        hub = await resolve(AppConfig(), api)
        found_at = hub.discovery.last_success_at

        api.error = RuntimeError("HTTP 503 TEMPORARILY_UNAVAILABLE")
        assert await hub.refresh_job_ids() is False

        assert hub.config.job_ids == {"mcmc": 1}, "a blip must not take the models away"
        assert hub.job_ids_source == "discovered"
        assert "job_ids" not in hub.degraded, "the map is not absent, only possibly stale"
        assert "503" in hub.degraded["job_discovery"]
        assert "503" in hub.discovery.last_error
        assert hub.discovery.last_error_at is not None
        assert hub.discovery.last_success_at == found_at
        assert hub.discovery.failures == 1

    async def test_an_empty_answer_after_a_good_one_keeps_the_map(self):
        """All of ours vanishing at once is far likelier to be lost access or
        a mistyped tag than every job being deleted."""
        api = FakeJobsApi([tagged(1, "mcmc")])
        hub = await resolve(AppConfig(), api)

        api.jobs = [job(9, "someone else's")]
        await hub.refresh_job_ids()

        assert hub.config.job_ids == {"mcmc": 1}
        assert "1 jobs visible" in hub.degraded["job_discovery"]

    async def test_a_later_success_clears_degraded_but_keeps_the_last_error(self):
        api = FakeJobsApi([tagged(1, "mcmc")])
        hub = await resolve(AppConfig(), api)
        api.error = RuntimeError("boom")
        await hub.refresh_job_ids()

        api.error = None
        await hub.refresh_job_ids()

        assert "job_discovery" not in hub.degraded
        assert hub.discovery.last_error is not None, "a flapping refresh stays visible"
        assert hub.discovery.last_success_at >= hub.discovery.last_error_at

    async def test_ambiguity_is_cleared_once_it_is_resolved(self):
        api = FakeJobsApi([tagged(4, "scenario"), tagged(9, "scenario")])
        hub = await resolve(AppConfig(), api)
        assert "job_ids_ambiguous" in hub.degraded

        api.jobs = [tagged(9, "scenario")]
        await hub.refresh_job_ids()
        assert "job_ids_ambiguous" not in hub.degraded

    async def test_healthz_reports_the_last_error_and_the_last_success(self, app_and_hub):
        from fastapi.testclient import TestClient

        api = FakeJobsApi([tagged(1, "mcmc")])
        app, hub = app_and_hub(AppConfig(), jobs_api=api)
        await hub.refresh_job_ids()
        api.error = RuntimeError("HTTP 403 PERMISSION_DENIED")
        await hub.refresh_job_ids()

        body = TestClient(app).get("/api/healthz").json()
        assert body["job_ids"] == {"source": "discovered", "count": 1}
        assert "403" in body["discovery"]["last_error"]
        assert body["discovery"]["last_success_at"] is not None
        assert "job_discovery" in body["degraded"]


@pytest.fixture
def started(monkeypatch):
    """A hub through its real `startup()`, with the Jobs API faked.

    `startup()` builds its own JobsApi from the config, so the class is
    swapped rather than the attribute. Everything else it does degrades
    harmlessly offline: no Lakebase, no volume, no OAuth credentials.
    """
    from server import services

    async def _start(config: AppConfig, api: FakeJobsApi) -> ServiceHub:
        monkeypatch.setattr(services, "JobsApi", lambda *a, **k: api)
        hub = ServiceHub(config)
        await hub.startup()
        return hub

    return _start


class TestBackgroundRefresh:
    async def test_the_background_refresh_picks_up_a_new_job(self, started):
        api = FakeJobsApi([tagged(1, "mcmc")])
        hub = await started(AppConfig(discovery_refresh_s=0.01), api)
        try:
            assert hub.discovery.refresh_s == 0.01
            api.jobs = [tagged(1, "mcmc"), tagged(2, "scenario")]
            await eventually(api, lambda: "scenario" in hub.config.job_ids)
        finally:
            await hub.shutdown()

    async def test_a_failed_startup_discovery_is_recovered_without_a_restart(self, started):
        api = FakeJobsApi([tagged(1, "mcmc")], error=RuntimeError("HTTP 500"))
        hub = await started(AppConfig(discovery_refresh_s=0.01), api)
        try:
            assert hub.job_ids_source == "none"
            api.error = None
            await eventually(api, lambda: hub.job_ids_source == "discovered")
            assert "job_ids" not in hub.degraded
        finally:
            await hub.shutdown()

    async def test_the_refresh_task_is_cancelled_on_shutdown(self, started):
        api = FakeJobsApi([tagged(1, "mcmc")])
        hub = await started(AppConfig(discovery_refresh_s=0.01), api)
        task = hub._discovery_task
        assert task is not None and not task.done()

        await hub.shutdown()

        assert task.cancelled()
        assert hub._discovery_task is None
        calls = api.calls
        await asyncio.sleep(0.05)
        assert api.calls == calls, "nothing may call the Jobs API after shutdown"

    async def test_an_explicit_map_disables_the_refresh(self, started):
        """DBX_JOB_IDS is an allow-list: refreshing would widen it."""
        api = FakeJobsApi([tagged(1, "mcmc"), tagged(2, "scenario")])
        hub = await started(AppConfig(job_ids={"mcmc": 500}, discovery_refresh_s=0.01), api)
        try:
            assert hub._discovery_task is None
            assert hub.discovery.refresh_s == 0
            await asyncio.sleep(0.05)
            assert api.calls == 0
            assert hub.config.job_ids == {"mcmc": 500}
        finally:
            await hub.shutdown()

    async def test_zero_disables_the_refresh(self, started):
        api = FakeJobsApi([tagged(1, "mcmc")])
        hub = await started(AppConfig(discovery_refresh_s=0), api)
        try:
            assert hub._discovery_task is None
            assert api.calls == 1, "startup still discovers once"
        finally:
            await hub.shutdown()

    @pytest.mark.parametrize(
        ("raw", "expected"), [(None, 300.0), ("60", 60.0), ("0", 0.0), ("-5", 0.0)]
    )
    def test_the_interval_is_read_from_the_environment(self, raw, expected):
        env = {} if raw is None else {"DBX_DISCOVERY_REFRESH_S": raw}
        assert AppConfig.from_env(env).discovery_refresh_s == expected


class TestOnDemand:
    """A trigger naming a model that is not in the map costs one refresh."""

    async def test_an_unknown_model_triggers_one_refresh_and_is_found(self):
        api = FakeJobsApi([tagged(1, "mcmc")])
        hub = await resolve(AppConfig(discovery_on_demand_min_s=0), api)

        api.jobs = [tagged(1, "mcmc"), tagged(2, "scenario")]
        assert await hub.job_id_for("scenario") == 2
        assert api.calls == 2

    async def test_a_known_model_costs_no_lookup(self):
        api = FakeJobsApi([tagged(1, "mcmc")])
        hub = await resolve(AppConfig(discovery_on_demand_min_s=0), api)

        assert await hub.job_id_for("mcmc") == 1
        assert api.calls == 1

    async def test_it_is_rate_limited_across_callers(self):
        """Otherwise every request naming a nonexistent model would page
        through the workspace's whole job list."""
        api = FakeJobsApi([tagged(1, "mcmc")])
        hub = await resolve(AppConfig(discovery_on_demand_min_s=60), api)

        assert await hub.job_id_for("nope") is None
        assert await hub.job_id_for("also-nope") is None
        assert api.calls == 1, "startup's attempt is recent enough; no second lookup"

    async def test_an_explicit_map_is_never_widened_on_demand(self):
        api = FakeJobsApi([tagged(2, "scenario")])
        hub = await resolve(AppConfig(job_ids={"mcmc": 1}, discovery_on_demand_min_s=0), api)

        assert await hub.job_id_for("scenario") is None
        assert api.calls == 0

    def test_the_trigger_route_finds_a_job_created_after_startup(self, app_and_hub):
        from fastapi.testclient import TestClient

        class Jobs(FakeJobsApi):
            launched: int | None = None

            async def run_now(self, job_id, params):
                self.launched = job_id
                return 1234

        api = Jobs([tagged(2, "scenario")])
        app, _ = app_and_hub(AppConfig(discovery_on_demand_min_s=0), jobs_api=api)

        response = TestClient(app).post("/api/runs", json={"model": "scenario"})

        assert response.status_code == 202, response.text
        assert api.launched == 2

    def test_the_trigger_route_404s_after_one_refresh(self, app_and_hub):
        from fastapi.testclient import TestClient

        api = FakeJobsApi([tagged(1, "mcmc")])
        app, _ = app_and_hub(AppConfig(discovery_on_demand_min_s=0), jobs_api=api)

        response = TestClient(app).post("/api/runs", json={"model": "nope"})

        assert response.status_code == 404
        assert "'dbx-leaning'" in response.json()["detail"]
        assert api.calls == 1
