"""A model, in a container, with the repo genuinely absent.

`tests/job/test_model_packaging.py` asserts most of this already, and asserts
it in a subprocess that inherits the repo root on `sys.path` — so it is
checking that a model does not USE the platform in an environment where the
platform is importable. That is the same shape of blind spot that let
`app/shared/` be deleted with 296 tests green.

Here the platform is not on disk. The image is built with `models/heartbeat/`
as its build context, so the Dockerfile cannot copy the repo in; and the tests
below prove the absence rather than assuming it, because a test whose premise
is silently false is worse than no test.
"""

from __future__ import annotations

from .harness import importable, probe, run

PLATFORM_MODULES = ["job", "shared", "server", "heartbeat"]

_DISTS = r"""
import json
from importlib.metadata import distributions
names = sorted({d.metadata["Name"].lower() for d in distributions() if d.metadata["Name"]})
print(json.dumps({"installed": names}))
"""


def _distributions(image: str) -> set[str]:
    return set(probe(image, _DISTS)["installed"])


def test_the_repo_really_is_absent(model_image):
    """The premise every other test in this file rests on.

    If this fails, nothing else here means what it says — the model would be
    passing because the platform happened to be importable, which is exactly
    the failure mode this file exists to close.
    """
    seen = importable(model_image, PLATFORM_MODULES)

    assert seen["heartbeat"] is True, "the model itself must import"
    for name in ("job", "shared", "server"):
        assert seen[name] == "ModuleNotFoundError", (
            f"{name!r} is importable inside the model container ({seen[name]}), so "
            f"this container is not proving isolation"
        )


def test_no_platform_source_reached_the_image(model_image):
    """Belt and braces: not importable AND not on disk.

    A future edit that adds `COPY ../../job /job` to the Dockerfile would fail
    the build (the context forbids it), but a `pip install` of something that
    vendored the harness would not. This notices that.
    """
    found = probe(
        model_image,
        r"""
import json, pathlib
suspects = []
for p in pathlib.Path("/").rglob("run_model.py"):
    suspects.append(str(p))
for name in ("job", "shared", "server"):
    for p in pathlib.Path("/usr/local/lib/python3.11/site-packages").glob(name):
        suspects.append(str(p))
print(json.dumps({"suspects": suspects[:20]}))
""",
    )
    assert found["suspects"] == [], f"platform source found in a model image: {found['suspects']}"


def test_the_model_needs_no_dependency_at_all(model_image, base):
    """`dependencies = []` in models/heartbeat/pyproject.toml, made a fact.

    The heartbeat is trivial, but this is not a test about the heartbeat. It is
    the proof that the model CONTRACT costs a model nothing: if the wrapper ever
    starts requiring a base class, a decorator or a registry import, the
    cheapest possible model stops being installable on its own and this fails.

    The comparison is against the BASE image rather than a hardcoded allow-list,
    because the interesting quantity is what the model ADDED. A base image ships
    pip, setuptools and whatever they drag in (`packaging`, today), and pinning
    that list here would turn a base image bump into a spurious failure about
    the model contract.
    """
    installed = _distributions(model_image)
    baseline = _distributions(base)

    added = installed - baseline
    assert added == {"dbx-model-heartbeat"}, (
        f"installing the heartbeat added {sorted(added)}; it declares no "
        f"dependencies, so anything beyond the distribution itself means the "
        f"platform has leaked into the model contract"
    )
    # Named explicitly because it is the one that would mean the envelope
    # leaked: pydantic is shared/'s dependency and no model's business.
    assert "pydantic" not in installed


def test_the_entry_point_is_discoverable_without_the_harness(model_image):
    """Discovery is `importlib.metadata`, so it works with no platform present.

    This is what makes "a model in another repository is found identically"
    true rather than aspirational: the metadata is the model's, and nothing
    here had to be told where the model lives.
    """
    found = probe(
        model_image,
        r"""
import json
from importlib.metadata import entry_points
eps = {ep.name: ep.value for ep in entry_points(group="dbx_leaning.models")}
print(json.dumps({"eps": eps}))
""",
    )["eps"]
    assert found == {"heartbeat": "heartbeat:build_model"}


def test_the_model_runs_with_a_hand_written_emit(model_image):
    """The whole coupling surface, exercised with the platform absent.

    A model is handed `emit` and `should_cancel` and knows nothing else. If
    that is true, a caller can be twenty lines of stdlib — which is what a
    model author debugging in a notebook actually has.
    """
    out = probe(
        model_image,
        r"""
import json
from importlib.metadata import entry_points

(ep,) = entry_points(group="dbx_leaning.models")
model = ep.load()({"seconds": 0.3, "hz": 10})

messages = []
model.attach(emit=lambda t, **f: messages.append({"type": t, **f}),
             should_cancel=lambda: False)
status = model.run()

print(json.dumps({
    "status": status,
    "types": sorted({m["type"] for m in messages}),
    "count": len(messages),
}))
""",
    )
    assert out["status"] == "SUCCEEDED"
    assert out["count"] > 0
    assert "progress" in out["types"], out["types"]


def test_cancellation_is_the_models_own_business(model_image):
    """`should_cancel` is a callable the model polls — no token type, no import.

    Proving this without the platform is the point: if cancellation required
    `from job.cancellation import CancellationToken`, a model could not be
    tested alone and this would fail to import.
    """
    out = probe(
        model_image,
        r"""
import json
from importlib.metadata import entry_points

(ep,) = entry_points(group="dbx_leaning.models")
model = ep.load()({"seconds": 60, "hz": 50})

seen = []
calls = {"n": 0}
def should_cancel():
    calls["n"] += 1
    return calls["n"] > 3

model.attach(emit=lambda t, **f: seen.append(t), should_cancel=should_cancel)
status = model.run()
print(json.dumps({"status": status, "polls": calls["n"], "messages": len(seen)}))
""",
    )
    assert out["status"] == "CANCELLED", out
    assert out["polls"] <= 10, "the model ran on well past the cancel"


def test_a_model_container_needs_no_network(model_image):
    """Run with `--network none`, which is `harness.run`'s default.

    Free Edition restricts outbound egress to trusted domains, so a model that
    quietly fetched something at run time would fail on the workspace and
    nowhere else. This is the cheap place to find that out.
    """
    result = run(
        model_image,
        "from importlib.metadata import entry_points\n"
        "(ep,) = entry_points(group='dbx_leaning.models')\n"
        "m = ep.load()({'seconds': 0.2, 'hz': 10})\n"
        "m.attach(emit=lambda *a, **k: None, should_cancel=lambda: False)\n"
        "print(m.run())",
        network="none",
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert "SUCCEEDED" in result.stdout


def test_importing_the_model_loads_only_the_model(model_image):
    """No transitive platform import, measured where there is nothing to import.

    The subprocess version of this test lives in tests/job/test_model_packaging.py
    and is the faster one to run; this is the one that cannot be fooled.
    """
    out = probe(
        model_image,
        r"""
import json, sys
before = set(sys.modules)
import heartbeat  # noqa: F401

# Stdlib is filtered out, not asserted on. A fresh interpreter has not yet
# imported `typing` or `collections.abc`, so a model that type-annotates
# anything pulls them in here and does not in a warm process — which says
# nothing about coupling. What matters is that nothing THIRD-PARTY arrives.
loaded = sorted(
    m for m in set(sys.modules) - before
    if not m.startswith("_") and m.partition(".")[0] not in sys.stdlib_module_names
)
print(json.dumps({"loaded": loaded}))
""",
    )
    assert out["loaded"] == ["heartbeat", "heartbeat.model"], out["loaded"]
