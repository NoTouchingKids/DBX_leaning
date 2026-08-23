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

#: library -> the pyproject extra that provides it. Two models sharing an
#: extra legitimately share its libraries, so the property to assert is not
#: "appears nowhere else" but "appears in exactly the environments entitled
#: to it" — which still catches a real leak and no longer punishes reuse.
LIBRARY_OWNER = {
    "gurobipy": "gurobi",
    "scikit-learn": "forecasting",
    "emcee": "mcmc",
    "torch": "nn",
}


def registry() -> dict[str, str]:
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    from _registry import model_extras

    return model_extras()


def pins(path: pathlib.Path) -> dict[str, str]:
    found = {}
    for line in path.read_text().splitlines():
        match = re.match(r"^([A-Za-z0-9._-]+)==([^\s;]+)", line.strip())
        if match:
            found[match.group(1).lower()] = match.group(2)
    return found


@pytest.mark.parametrize("library", sorted(LIBRARY_OWNER))
def test_a_library_reaches_exactly_the_models_entitled_to_it(library):
    """The microservice property. A model's library must not appear in an
    environment whose extra does not provide it — and must appear in every
    environment whose extra does."""
    owning_extra = LIBRARY_OWNER[library]
    entitled = {m for m, extra in registry().items() if extra == owning_extra}
    carrying = {
        path.stem for path in REQUIREMENTS.glob("*.txt") if library in pins(path)
    }
    assert carrying == entitled, (
        f"{library} is in {sorted(carrying)} but entitled: {sorted(entitled)}"
    )


def test_two_models_may_share_an_extra():
    """Reuse is allowed and currently used — the two Gurobi models share one
    dependency set. This exists so the test above is not misread as banning it."""
    extras = registry()
    shared = [e for e in set(extras.values()) if list(extras.values()).count(e) > 1]
    assert "gurobi" in shared, extras


def test_every_model_has_an_environment_file():
    for model in registry():
        assert (REQUIREMENTS / f"{model}.txt").exists(), f"{model} has no requirements file"


def test_the_app_carries_no_model_libraries_at_all():
    installed = pins(ROOT / "requirements.txt")
    for library in LIBRARY_OWNER:
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
