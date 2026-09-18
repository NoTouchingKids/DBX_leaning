"""Find this platform's jobs in the workspace, when the environment did not say.

``DBX_JOB_IDS`` is the normal path: ``resources/app.yml`` interpolates
``${resources.jobs.model_scenario.id}`` and friends, so the app is told the ids
by the same bundle that created the jobs.

That path has one failure mode, and it is silent. The env var reaches the app
only when the running app *deployment* was created by the bundle. Deploy
without ``bundle run``, or redeploy from the Apps UI, and the app comes up with
the environment of whichever deployment is actually live — which, if that one
came from the UI, is ``app/app.yaml``, and ``app/app.yaml`` deliberately has no
``DBX_JOB_IDS`` because a hand deploy has no bundle to interpolate from. The
result is an app that works in every respect except that ``/api/models`` is
empty and nothing can be triggered.

So the app asks the workspace instead. Every job this bundle creates carries
``tags: {project: dbx-leaning, model: <name>}`` and is named
``... dbx-leaning · <name>``, either of which identifies it. Discovery is a
FALLBACK, not the primary: an explicit ``DBX_JOB_IDS`` always wins, because it
is also the allow-list and someone who set it meant it.

Everything here is a pure function over the Jobs API's response so it can be
tested against both response shapes without a workspace.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = ["PROJECT_TAG", "map_jobs_to_models", "DiscoveryResult"]

#: The tag every job in `resources/model_*.job.yml` carries.
PROJECT_TAG = "dbx-leaning"

#: Fallback when tags are absent from the response. The job name is
#: `"[${bundle.target}] dbx-leaning · scenario"`, and `mode: development` adds
#: its own `[dev <user>] ` prefix on top — so match the tail, not the whole.
#:
#: The `·` is doing real work: it is what makes this OUR job rather than any
#: job whose name happens to contain the project name.
_NAME = re.compile(r"dbx-leaning\s*·\s*([a-z][a-z0-9_]*)\s*$")


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


def _model_of(job: dict[str, Any]) -> str | None:
    """Which model this job runs, by tag if the API said, else by name.

    Two routes because the Jobs list response is not guaranteed to carry tags —
    `expand_tasks=false` trims `settings`, and what survives has varied. Rather
    than depend on that, take the tag when it is there and fall back to the
    name, which is always present.
    """
    settings = job.get("settings") or {}
    tags = settings.get("tags") or {}
    if tags.get("project") == PROJECT_TAG:
        model = tags.get("model")
        if isinstance(model, str) and model:
            return model

    match = _NAME.search(str(settings.get("name") or ""))
    return match.group(1) if match else None


def map_jobs_to_models(jobs: list[dict[str, Any]]) -> DiscoveryResult:
    """model name -> job id, from a Jobs API list response.

    Where more than one job claims a model, the highest id wins — the most
    recently created, which is the one a re-deploy just made. Deterministic
    rather than correct: there is no way to tell `dev` from `prod` from the
    outside, which is why the loser is reported instead of dropped.
    """
    candidates: dict[str, list[int]] = {}
    for job in jobs:
        model = _model_of(job)
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
