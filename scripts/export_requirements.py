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
#: Notably NOT `delta`: the durable writer is Spark, which the runtime already
#: provides, and the delta-rs writer is not implemented (see job/delta.py).
#: Add it back in the same commit that makes DeltaRsWriter real.
ENVIRONMENTS: dict[str, list[str]] = {
    model: ["job", extra] for model, extra in sorted(model_extras().items())
}

#: The app is not a model, and does not get a Delta writer or any model
#: library — it observes, it does not compute.
APP_EXTRAS = ["app"]

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


def export(extras: list[str]) -> str:
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
    return body + "\n"


def render(name: str, extras: list[str]) -> str:
    return HEADER.format(name=name, extras=", ".join(extras)) + "\n" + export(extras)


def targets() -> dict[pathlib.Path, str]:
    out = {OUT_DIR / f"{name}.txt": render(name, extras) for name, extras in ENVIRONMENTS.items()}
    # The app's list lives in `app/`, not at the repo root: Databricks Apps
    # installs requirements.txt from the app's SOURCE directory, and
    # `resources/app.yml` points that at `../app`.
    out[ROOT / "app" / "requirements.txt"] = render("databricks-app", APP_EXTRAS)
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
