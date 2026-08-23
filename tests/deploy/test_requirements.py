"""Per-model dependency isolation, and that the exported files still match
the lock. A generated file that has drifted means the thing deployed is not
the thing tested.
"""

from __future__ import annotations

import pathlib
import re
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
REQUIREMENTS = ROOT / "deploy" / "requirements"

#: The library that makes each model what it is, and must not leak elsewhere.
SIGNATURE = {
    "gurobi_scheduling": "gurobipy",
    "forecasting": "scikit-learn",
    "mcmc": "emcee",
}


def pins(path: pathlib.Path) -> dict[str, str]:
    found = {}
    for line in path.read_text().splitlines():
        match = re.match(r"^([A-Za-z0-9._-]+)==([^\s;]+)", line.strip())
        if match:
            found[match.group(1).lower()] = match.group(2)
    return found


@pytest.mark.parametrize("model", sorted(SIGNATURE))
def test_a_models_own_library_is_in_its_environment(model):
    assert SIGNATURE[model] in pins(REQUIREMENTS / f"{model}.txt")


@pytest.mark.parametrize("model", sorted(SIGNATURE))
def test_a_models_library_leaks_into_no_other_environment(model):
    """The microservice property, asserted rather than assumed: the MCMC job
    must not be carrying gurobipy."""
    library = SIGNATURE[model]
    for other in REQUIREMENTS.glob("*.txt"):
        if other.stem == model:
            continue
        assert library not in pins(other), f"{library} leaked into {other.name}"


def test_the_app_carries_no_model_libraries_at_all():
    installed = pins(ROOT / "requirements.txt")
    for library in SIGNATURE.values():
        assert library not in installed, f"the app should not install {library}"
    assert "deltalake" not in installed, "the app never writes Delta"
    assert "fastapi" in installed


def test_every_environment_pins_the_same_version_of_a_shared_dependency():
    """One resolution, many subsets. Two environments disagreeing about
    pydantic would mean the lock was bypassed somewhere."""
    versions: dict[str, set[str]] = {}
    for path in [*REQUIREMENTS.glob("*.txt"), ROOT / "requirements.txt"]:
        for package, version in pins(path).items():
            versions.setdefault(package, set()).add(version)
    conflicts = {p: v for p, v in versions.items() if len(v) > 1}
    assert not conflicts, f"version drift across environments: {conflicts}"


def test_nothing_is_left_unpinned():
    for path in [*REQUIREMENTS.glob("*.txt"), ROOT / "requirements.txt"]:
        for line in path.read_text().splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            assert "==" in stripped, f"{path.name}: unpinned requirement {stripped!r}"


def test_the_generated_files_still_match_the_lock():
    result = subprocess.run(
        ["uv", "run", "python", "scripts/export_requirements.py", "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_aggregate_models_extra_excludes_the_heavy_one():
    """`pip install dbx-leaning[models]` must not pull torch.

    torch plus its CUDA stack is around 4GB. Adding `nn` to the aggregate
    would be a one-word change that quietly defeats the per-model split for
    everyone who only wanted the light models — so it is asserted, not left
    to a comment.
    """
    import tomllib

    with (ROOT / "pyproject.toml").open("rb") as fh:
        extras = tomllib.load(fh)["project"]["optional-dependencies"]

    aggregate = " ".join(extras["models"])
    assert "nn" not in aggregate.replace("dbx-leaning", "").split(","), aggregate
    assert any("torch" in dep for dep in extras["nn"]), "the nn extra should carry torch"


def test_torch_reaches_exactly_one_job_environment():
    """The claim the whole microservice split rests on."""
    carrying = [p.name for p in REQUIREMENTS.glob("*.txt") if "torch" in pins(p)]
    assert carrying == ["neural_net.txt"], carrying
    assert "torch" not in pins(ROOT / "requirements.txt"), "the app must not install torch"
