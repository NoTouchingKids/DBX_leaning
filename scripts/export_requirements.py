#!/usr/bin/env python3
"""Generate the per-environment requirements files from ``uv.lock``.

Each model is deployed as its own job with its own serverless environment, so
each gets its own dependency list — the whole point of the microservice split
is that the MCMC job does not carry gurobipy, and a model that later needs GPU
libraries does not impose them on the other four.

Everything here is *exported from the lock*, never re-resolved. A generated
file that disagrees with ``uv.lock`` would mean the thing deployed is not the
thing tested; ``tests/deploy/test_requirements.py`` fails if that
happens.

    uv run python scripts/export_requirements.py          # write
    uv run python scripts/export_requirements.py --check  # verify, write nothing
"""

from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from _registry import model_extras  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "deploy" / "requirements"

#: environment name -> the extras that make it up, derived from the single
#: registry in pyproject.toml rather than repeated here.
#:
#: Every job carries `job` (the harness's own transport) plus its own model
#: extra, and nothing else.
#:
#: There is deliberately no `delta` extra to exclude any more: the durable
#: writer is Spark, which the runtime already provides, and the delta-rs
#: writer was deleted rather than left unimplemented (see job/delta.py).
ENVIRONMENTS: dict[str, list[str]] = {
    model: ["job", extra] for model, extra in sorted(model_extras().items())
}

#: The app is not a model, and does not get a Delta writer or any model
#: library — it observes, it does not compute.
APP_EXTRAS = ["app"]

#: Packages that must come from the Databricks serverless runtime, withheld so
#: pip leaves the runtime's copies alone. A pin here does not ADD a library, it
#: REPLACES the one the runtime already wired pyspark against.
#:
#: Deliberately NOT here:
#:   scipy, scikit-learn — see RESOLVE_AT_INSTALL below. Withholding those was
#:     the wrong half of the same lesson: safe when the runtime has them,
#:     an ImportError when it does not.
#:   torch      — the heavy one, and the reason per-model environments exist.
#:     It bundles its own numpy interop rather than linking a system ABI, and
#:     the runtime is not guaranteed to carry it at all.
#:   emcee, joblib, threadpoolctl — pure Python. Nothing to mismatch.
#:   typing-extensions — pydantic v2 needs a recent one, and an older runtime
#:     copy breaks it in a way that reads as a pydantic bug.
RUNTIME_PROVIDED: frozenset[str] = frozenset(
    {
        # The two the runtime cannot be without: pyspark imports pandas, and
        # pandas is built against numpy. Replacing either breaks pyspark, not
        # us, which is why these are withheld outright rather than relaxed.
        "numpy",
        "pandas",
        # Pure-Python staples the runtime always has. No ABI to mismatch;
        # withheld only to stop pip churning versions it does not need to.
        "python-dateutil",
        "setuptools",
        "six",
        "tzdata",
    }
)

#: Packages emitted by NAME ONLY, with the lock's `==` dropped, so pip resolves
#: them at install time against whatever the runtime already has.
#:
#: **This is the middle ground between pinning and withholding, and both ends
#: burned this repo once.** Pinning `scipy==1.18.1` installs OUR scipy over the
#: runtime's, compiled against a numpy ABI that may not be the one loaded —
#: which does not raise, it calls abort(). A task died on `exit code 134
#: (SIGABRT)` with no traceback, because by then there was no Python left to
#: raise one. Withholding it instead is safe only while the runtime happens to
#: carry it; the day it does not, the model fails at import.
#:
#: An unpinned name has neither failure. pip treats ANY installed version as
#: satisfying it and installs nothing, so the runtime's copy and its ABI are
#: left exactly as found; and when the runtime does not have it, pip resolves
#: a version compatible with the numpy that is there.
#:
#: No lower bound on purpose. A floor is the one thing that could make pip
#: replace the runtime's copy again, which is the whole failure being designed
#: out — and the versions Databricks serverless ships are far above any floor
#: this project would write.
RESOLVE_AT_INSTALL: frozenset[str] = frozenset(
    {
        "scipy",
        "scikit-learn",
    }
)


def _requirement_name(line: str) -> str:
    """The bare package name from an exported requirement line."""
    name = line.strip()
    for separator in ("==", ">=", "<=", "~=", "!=", ">", "<", ";", "[", " "):
        name = name.split(separator)[0]
    return name.strip().lower()


def apply_runtime_policy(body: str) -> str:
    """Drop the withheld pins, relax the resolve-at-install ones.

    `uv export` writes a requirement then indents its provenance underneath, so
    removing or rewriting the requirement alone would leave orphaned comments
    attached to whatever came next — which reads as though the wrong package
    pulled it in, and is exactly the sort of stale comment this repo keeps
    paying for. A dropped requirement takes its `# via` block with it; a
    relaxed one keeps its own.

    The lock can also resolve one package to several versions behind
    environment markers (scipy is two: one for <3.12, one for >=3.12). Relaxed
    to a bare name those collapse into the same line, so only the first
    survives — the marker existed to choose a version, and there is no longer a
    version to choose.
    """
    out: list[str] = []
    skipping = False
    relaxed: set[str] = set()
    for line in body.splitlines():
        if line.startswith((" ", "\t")) and line.lstrip().startswith("#"):
            if skipping:
                continue
            out.append(line)
            continue
        name = _requirement_name(line) if line.strip() else ""
        if name in RUNTIME_PROVIDED or (name in RESOLVE_AT_INSTALL and name in relaxed):
            skipping = True
            continue
        skipping = False
        if name in RESOLVE_AT_INSTALL:
            relaxed.add(name)
            out.append(name)
            continue
        out.append(line)
    return "\n".join(out).strip() + "\n"


#: The job unit's own baseline: the harness transport, no model library.
#:
#: Nothing installs this today — each job task installs
#: `deploy/requirements/<model>.txt`, which is this plus exactly one model
#: extra, because the whole point of the split is that the MCMC job does not
#: carry gurobipy. It is written into `job/` so that folder states its own
#: floor the way `app/` does, and so `job/` is a complete unit the moment it
#: is packaged as a wheel.
JOB_EXTRAS = ["job"]

HEADER = """\
# GENERATED — do not edit.
#
# Exported from uv.lock by scripts/export_requirements.py, so what deploys is
# exactly what the tests ran against. To change it, edit pyproject.toml, run
# `uv lock`, then re-run that script.
#
# environment: {name}
# extras: {extras}
"""


def export(extras: list[str], *, strip_runtime: bool = True) -> str:
    """One `uv export` for one environment.

    No hashes: Databricks' serverless environment installer takes a plain
    requirement list, and hash lines are noise it does not use. Versions are
    still exact — the pinning comes from the lock, not from the hashes.
    """
    cmd = [
        "uv",
        "export",
        "--no-dev",
        "--no-hashes",
        "--no-emit-project",  # the code arrives by workspace sync, not pip
        "--format",
        "requirements-txt",
    ]
    for extra in extras:
        cmd += ["--extra", extra]
    result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"uv export failed for {extras}:\n{result.stderr}")
    # Drop uv's own provenance header; ours says more.
    body = "\n".join(
        line for line in result.stdout.splitlines() if not line.startswith("#")
    ).strip()
    return apply_runtime_policy(body) if strip_runtime else body + "\n"


def render(name: str, extras: list[str], *, strip_runtime: bool = True) -> str:
    return (
        HEADER.format(name=name, extras=", ".join(extras))
        + "\n"
        + export(extras, strip_runtime=strip_runtime)
    )


def targets() -> dict[pathlib.Path, str]:
    out = {OUT_DIR / f"{name}.txt": render(name, extras) for name, extras in ENVIRONMENTS.items()}
    # The app's list lives in `app/`, not at the repo root: Databricks Apps
    # installs requirements.txt from the app's SOURCE directory, and
    # `resources/app.yml` points that at `../app`.
    # Not stripped: the app runs on Databricks Apps, a plain Python
    # environment, not the serverless Spark runtime whose preinstalled
    # scientific stack RUNTIME_PROVIDED and RESOLVE_AT_INSTALL are about.
    out[ROOT / "app" / "requirements.txt"] = render(
        "databricks-app", APP_EXTRAS, strip_runtime=False
    )
    # The job unit's baseline — see JOB_EXTRAS. Every file under
    # deploy/requirements/ is this plus one model extra.
    out[ROOT / "job" / "requirements.txt"] = render("job-harness", JOB_EXTRAS)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="verify files match the lock; write nothing"
    )
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stale: list[str] = []

    for path, content in targets().items():
        rel = path.relative_to(ROOT)
        if args.check:
            current = path.read_text() if path.exists() else ""
            if current != content:
                stale.append(str(rel))
            continue
        path.write_text(content)
        print(f"wrote {rel} ({len(content.splitlines())} lines)")

    if args.check:
        if stale:
            print("out of sync with uv.lock:", ", ".join(stale), file=sys.stderr)
            print("run: uv run python scripts/export_requirements.py", file=sys.stderr)
            return 1
        print("all requirements files match uv.lock")
    return 0


if __name__ == "__main__":
    sys.exit(main())
