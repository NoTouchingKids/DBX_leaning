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
#: The app installs from its own SOURCE directory — `app/`, which is what
#: `resources/app.yml` gives Databricks Apps — not from the repo root.
APP_REQUIREMENTS = ROOT / "app" / "requirements.txt"
#: The job unit's baseline. Nothing installs it today — each task installs its
#: own `deploy/requirements/<model>.txt`, which is this plus one model extra —
#: but it is what `job/` states about itself, so it must stay honest.
JOB_REQUIREMENTS = ROOT / "job" / "requirements.txt"

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


def requirements(path: pathlib.Path) -> set[str]:
    """Every package the file asks for, pinned or not.

    `pins()` reads versions and so cannot see a deliberately unpinned line.
    Presence and version are two different questions now, and the microservice
    property is about presence.
    """
    names = set()
    for line in path.read_text().splitlines():
        if not line.strip() or line.startswith((" ", "\t", "#")):
            continue
        names.add(re.split(r"[=<>~!;\[ ]", line.strip())[0].lower())
    return names


@pytest.mark.parametrize("library", sorted(LIBRARY_OWNER))
def test_a_library_reaches_exactly_the_models_entitled_to_it(library):
    """The microservice property. A model's library must not appear in an
    environment whose extra does not provide it — and must appear in every
    environment whose extra does."""
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    from export_requirements import RUNTIME_PROVIDED

    owning_extra = LIBRARY_OWNER[library]
    entitled = {m for m, extra in registry().items() if extra == owning_extra}
    carrying = {p.stem for p in REQUIREMENTS.glob("*.txt") if library in requirements(p)}

    if library in RUNTIME_PROVIDED:
        # Entitled to it, and still must not appear. Withholding is for what
        # the runtime is guaranteed to have and pyspark is wired against, so
        # the assertion inverts rather than disappearing. Nothing in the table
        # above is withheld today; this branch is what makes moving one here a
        # deliberate act rather than a silent one.
        assert carrying == set(), (
            f"{library} comes from the serverless runtime, so no environment "
            f"may carry it — found in {sorted(carrying)}. If a model genuinely "
            f"needs it, RESOLVE_AT_INSTALL is the safer half of the choice; "
            f"see scripts/export_requirements.py."
        )
        return

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
    installed = pins(APP_REQUIREMENTS)
    for library in LIBRARY_OWNER:
        assert library not in installed, f"the app should not install {library}"
    assert "deltalake" not in installed, "the app never writes Delta"
    assert "fastapi" in installed


def test_every_environment_pins_the_same_version_of_a_shared_dependency():
    """One resolution, many subsets. Two environments disagreeing about
    pydantic would mean the lock was bypassed somewhere."""
    versions: dict[str, set[str]] = {}
    for path in [*REQUIREMENTS.glob("*.txt"), APP_REQUIREMENTS, JOB_REQUIREMENTS]:
        for package, version in pins(path).items():
            versions.setdefault(package, set()).add(version)
    conflicts = {p: v for p, v in versions.items() if len(v) > 1}
    assert not conflicts, f"version drift across environments: {conflicts}"


def test_nothing_is_left_unpinned_except_by_decision():
    """Everything is exact except the handful named in RESOLVE_AT_INSTALL.

    The lock is the source of truth for versions, so an unpinned line is
    normally drift. The exception is deliberate and narrow — see the comment on
    RESOLVE_AT_INSTALL — and this asserts the exception stays that narrow
    rather than becoming a habit.
    """
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    from export_requirements import RESOLVE_AT_INSTALL

    for path in [*REQUIREMENTS.glob("*.txt"), APP_REQUIREMENTS, JOB_REQUIREMENTS]:
        for line in path.read_text().splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.lower() in RESOLVE_AT_INSTALL:
                continue
            assert "==" in stripped, f"{path.name}: unpinned requirement {stripped!r}"


def test_the_numpy_linked_libraries_ship_unpinned_and_only_unpinned():
    """scipy and scikit-learn must appear by NAME, never with a version.

    A pin does not add a library on Databricks serverless, it replaces one —
    and both of these are compiled against a numpy ABI. Our copy loaded against
    the runtime's numpy does not raise, it calls abort(): the task dies on
    `exit code 134 (SIGABRT)` with no traceback. A bare name is satisfied by
    whatever is already installed, so pip touches nothing; if the runtime
    turns out not to have it, pip resolves one that fits the numpy that is
    there. Neither failure is reachable from an unpinned line, which is why
    this is asserted in both directions.
    """
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    from export_requirements import RESOLVE_AT_INSTALL

    for path in sorted(REQUIREMENTS.glob("*.txt")):
        for library in RESOLVE_AT_INSTALL:
            assert library not in pins(path), (
                f"{path.name} pins {library}, which overwrites the runtime's copy "
                "and can abort the task; it must be exported unpinned"
            )
        for library in requirements(path) & RESOLVE_AT_INSTALL:
            assert path.read_text().count(f"\n{library}\n") == 1, (
                f"{path.name} should carry {library} exactly once, as a bare name"
            )

    forecasting = requirements(REQUIREMENTS / "forecasting.txt")
    assert "scikit-learn" in forecasting, "the forecasting model imports it"
    assert "scipy" in forecasting, "scikit-learn needs it, and the lock knows"


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
    assert "torch" not in pins(APP_REQUIREMENTS), "the app must not install torch"


def test_the_job_baseline_carries_no_model_library():
    """`job/requirements.txt` is the harness's floor, not any model's.

    The microservice split only means something if the baseline is empty of
    solver and ML libraries: each task installs this plus exactly ONE model
    extra, so anything that leaks in here is carried by all ten jobs.
    """
    baseline = pins(JOB_REQUIREMENTS)
    for library in ("gurobipy", "ortools", "torch", "scikit-learn", "emcee", "pandas"):
        assert library not in baseline, (
            f"{library} in job/requirements.txt would be installed by every job, "
            "which is the thing the per-model split exists to avoid"
        )


def test_every_model_environment_is_the_job_baseline_plus_its_own():
    """Each `deploy/requirements/<model>.txt` must be a superset of the
    baseline. If a job dropped part of the harness, its telemetry would fail
    at run time rather than at deploy time."""
    baseline = set(pins(JOB_REQUIREMENTS))
    for path in sorted(REQUIREMENTS.glob("*.txt")):
        missing = baseline - set(pins(path))
        assert not missing, f"{path.name} is missing harness packages: {sorted(missing)}"


def test_the_model_environments_withhold_what_the_runtime_already_provides():
    """A pin here does not add a library, it REPLACES the runtime's.

    Databricks serverless installs numpy and wires its own pyspark and pandas
    against it. `numpy==2.4.6` in a requirements file overwrites that copy,
    and what breaks is not numpy — it is whatever else was built against the
    version that got replaced. Seven of the ten model environments carried one
    of these, which is why it showed up as most of the models rather than one.

    This is the strict half of the policy, and it applies to what the runtime
    cannot be without. Anything a model needs and the runtime merely *probably*
    has goes in RESOLVE_AT_INSTALL instead, which ships the name unpinned —
    satisfied by the runtime's copy when there is one, resolved by pip when
    there is not. `test_the_numpy_linked_libraries_ship_unpinned_and_only_
    unpinned` is that half.
    """
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    from export_requirements import RUNTIME_PROVIDED, _requirement_name

    offenders: list[str] = []
    for path in sorted((ROOT / "deploy" / "requirements").glob("*.txt")):
        for line in path.read_text().splitlines():
            if not line.strip() or line.startswith((" ", "#")):
                continue
            if _requirement_name(line) in RUNTIME_PROVIDED:
                offenders.append(f"{path.name}: {line.strip()}")
    assert not offenders, (
        "these override a library the serverless runtime already provides:\n  "
        + "\n  ".join(offenders)
    )
