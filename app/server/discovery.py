"""Find this platform's jobs in the workspace, by tag.

**Discovery is the mechanism, not a fallback** (``docs/v4-rewrite-plan.md``,
"Finding the jobs: a tag"). v3 had it the other way round — ``DBX_JOB_IDS``
interpolated from ``${resources.jobs.model_x.id}`` at deploy time, discovery
only for when that env var failed to arrive — and that tied the app's deploy
to the jobs'. A tag does not care where a job is defined, who deployed it, or
whether it still lives in this repository.

Every job carries ``tags: {project: <project tag>, model: <name>}`` and is
named ``... <project tag> · <name>``, either of which identifies it. The
project tag defaults to :data:`PROJECT_TAG` and is set per deployment with
``DBX_PROJECT_TAG``, so another team's instance of the app scopes to its own
jobs without forking this file.

An explicit ``DBX_JOB_IDS`` still wins, because it is also an allow-list and
someone who set it meant it.

The ServiceHub asks at startup and then again every ``DBX_DISCOVERY_REFRESH_S``
seconds (``services.py``), so a job created or recreated while the app is up
is found without a restart. That is a Jobs API list call — plain REST, never
the SQL warehouse, so a refresh costs no warehouse uptime.

Everything here is a pure function over the Jobs API's response so it can be
tested against both response shapes without a workspace.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

__all__ = ["PROJECT_TAG", "map_jobs_to_models", "DiscoveryResult", "DiscoveryStatus"]

#: The DEFAULT project tag — the one every job in `resources/model_*.job.yml`
#: carries. A deployment chooses its own with `DBX_PROJECT_TAG` (read by
#: `config.py`, which imports this as its default so the two cannot drift):
#: a team running its own instance of the app points it at its own jobs
#: without forking this module.
PROJECT_TAG = "dbx-leaning"


@lru_cache(maxsize=8)
def _name_pattern(project_tag: str) -> re.Pattern[str]:
    """Fallback when tags are absent from the response.

    The job name is `"[${bundle.target}] dbx-leaning · scenario"`, and
    `mode: development` adds its own `[dev <user>] ` prefix on top — so match
    the tail, not the whole.

    The `·` is doing real work: it is what makes this OUR job rather than any
    job whose name happens to contain the project name.

    Built from the configured tag rather than hardcoded, so the fallback
    scopes to the same project the tag does. A deployment configured for
    `team-x` that still matched `dbx-leaning · <model>` by name would pick up
    this project's jobs whenever the list response happened to omit tags —
    the exact cross-team leak a configurable tag exists to prevent.
    """
    return re.compile(rf"{re.escape(project_tag)}\s*·\s*([a-z][a-z0-9_]*)\s*$")


class DiscoveryResult:
    """What was found, and what was ambiguous about it.

    Ambiguity is carried rather than raised: two jobs matching one model is a
    normal consequence of deploying `dev` and `prod` into one workspace, and it
    should not stop the app from triggering. It is reported so that a
    deployment triggering the wrong one of the two is something you can see on
    `/healthz` rather than something you infer from a run that used the wrong
    environment.
    """

    def __init__(self, job_ids: dict[str, int], ambiguous: dict[str, list[int]]) -> None:
        self.job_ids = job_ids
        self.ambiguous = ambiguous

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"DiscoveryResult(job_ids={self.job_ids!r}, ambiguous={self.ambiguous!r})"


def _model_of(job: dict[str, Any], project_tag: str) -> str | None:
    """Which model this job runs, by tag if the API said, else by name.

    Two routes because the Jobs list response is not guaranteed to carry tags —
    `expand_tasks=false` trims `settings`, and what survives has varied. Rather
    than depend on that, take the tag when it is there and fall back to the
    name, which is always present.
    """
    settings = job.get("settings") or {}
    tags = settings.get("tags") or {}
    if tags.get("project") == project_tag:
        model = tags.get("model")
        if isinstance(model, str) and model:
            return model

    match = _name_pattern(project_tag).search(str(settings.get("name") or ""))
    return match.group(1) if match else None


def map_jobs_to_models(
    jobs: list[dict[str, Any]], project_tag: str = PROJECT_TAG
) -> DiscoveryResult:
    """model name -> job id, from a Jobs API list response.

    Only jobs belonging to ``project_tag`` count — by their ``project`` tag,
    or failing that by a name ending ``<project_tag> · <model>``.

    Where more than one job claims a model, the highest id wins — the most
    recently created, which is the one a re-deploy just made. Deterministic
    rather than correct: there is no way to tell `dev` from `prod` from the
    outside, which is why the loser is reported instead of dropped.
    """
    candidates: dict[str, list[int]] = {}
    for job in jobs:
        model = _model_of(job, project_tag)
        if model is None:
            continue
        try:
            job_id = int(job["job_id"])
        except (KeyError, TypeError, ValueError):
            continue
        candidates.setdefault(model, []).append(job_id)

    return DiscoveryResult(
        job_ids={model: max(ids) for model, ids in sorted(candidates.items())},
        ambiguous={model: sorted(ids) for model, ids in sorted(candidates.items()) if len(ids) > 1},
    )


@dataclass
class DiscoveryStatus:
    """How discovery is going, for `/healthz`.

    Mutable on purpose: the ServiceHub owns one and updates it on every
    refresh. Timestamps are wall-clock ISO-8601 UTC strings because they are
    for a human reading `/healthz`; the hub keeps its own monotonic clock for
    rate-limiting.

    ``last_error`` is NOT cleared by a later success: it is the most recent
    failure, with its time, so a refresh that flaps is visible after the fact.
    Whether it is *current* is the comparison of ``last_error_at`` against
    ``last_success_at`` — and the hub's ``degraded["job_discovery"]`` entry,
    which is what does clear on success.
    """

    project_tag: str = PROJECT_TAG
    #: Seconds between background refreshes; 0 when refresh is off, which is
    #: also what an explicit DBX_JOB_IDS forces.
    refresh_s: float = 0.0
    refreshes: int = 0
    failures: int = 0
    last_success_at: str | None = None
    last_error: str | None = None
    last_error_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_tag": self.project_tag,
            "refresh_s": self.refresh_s,
            "refreshes": self.refreshes,
            "failures": self.failures,
            "last_success_at": self.last_success_at,
            "last_error": self.last_error,
            "last_error_at": self.last_error_at,
        }
