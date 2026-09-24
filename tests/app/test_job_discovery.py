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
        self._jobs = jobs or []
        self._error = error
        self.calls = 0

    async def list_jobs(self, **_):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._jobs

    async def close(self): ...


async def resolve(config: AppConfig, jobs_api) -> ServiceHub:
    hub = ServiceHub(config)
    hub.jobs_api = jobs_api
    await hub._resolve_job_ids(config)
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
        await hub._resolve_job_ids(hub.config)

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
