"""A Databricks job task, as closely as a container can mirror one.

The three lines in `Dockerfile.job` are the three `dependencies` entries in
`resources/model_heartbeat.job.yml`, in order. So a green run here says the
declared dependency list is right — and if the deploy then fails, the fault is
in the workspace and not in what was declared.

`/src` is deleted after the installs. That is the point of the exercise rather
than tidiness: what runs on Databricks is the installed distribution, and a
container that kept the source could pass on an accidental relative import.
"""

from __future__ import annotations

import pathlib

import yaml

from .harness import probe, run_cmd

ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_the_source_tree_is_gone_and_everything_still_imports(job_image):
    """The premise. `shared` in particular: it lives under `app/` in the repo
    and reaches the job only because `[tool.setuptools] package-dir` maps it
    into this distribution. If that mapping ever breaks, it breaks here."""
    found = probe(
        job_image,
        r"""
import json, pathlib
out = {"src_exists": pathlib.Path("/src").exists()}
for name in ("job", "shared", "heartbeat"):
    try:
        mod = __import__(name)
        out[name] = mod.__file__
    except Exception as exc:
        out[name] = f"{type(exc).__name__}: {exc}"
print(json.dumps(out))
""",
    )
    assert found["src_exists"] is False, "the source tree is still there; this proves nothing"
    for name in ("job", "shared", "heartbeat"):
        assert "site-packages" in str(found[name]), f"{name} resolved to {found[name]!r}"


def test_a_run_completes_and_writes_its_part_files(job_image):
    """The entrypoint a task runs, end to end, with no app listening.

    `--network none`: a run with nowhere to send telemetry is the NORMAL case,
    not a degraded one — apps run about eight hours a day and jobs do not. If
    the harness ever grows a hard dependency on the live channel, this is where
    it shows up.
    """
    result = run_cmd(
        job_image,
        ["python", "-m", "job.main"],
        env={
            "DBX_MODEL": "heartbeat",
            "DBX_RUN_ID": "container-1",
            "DBX_MODEL_CONFIG": '{"seconds": 0.4, "hz": 10}',
            "DBX_TELEMETRY_VOLUME": "/tmp/telemetry",
        },
        network="none",
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined[-3000:]
    assert "SUCCEEDED" in combined, combined[-2000:]
    assert "unflushed=0" in combined, "records were left unwritten but the run claimed success"


def test_the_run_is_readable_back_off_the_volume(job_image):
    """A run whose telemetry cannot be read back has not really succeeded.

    This is the ingestion job's view of a run: JSONL part files under
    `runs/<run_id>/`, every record carrying the seq the JOB assigned. Reading
    them with nothing but stdlib is deliberate — the ingestion side is a
    separate service and should need no library of ours.
    """
    found = probe(
        job_image,
        r"""
import json, os, pathlib, subprocess, sys
env = dict(os.environ,
           DBX_MODEL="heartbeat", DBX_RUN_ID="container-2",
           DBX_MODEL_CONFIG='{"seconds": 0.4, "hz": 10}',
           DBX_TELEMETRY_VOLUME="/tmp/telemetry")
subprocess.run([sys.executable, "-m", "job.main"], env=env, check=True,
               capture_output=True)

run_dir = pathlib.Path("/tmp/telemetry/runs/container-2")
records = []
for part in sorted(run_dir.glob("part-*.jsonl")):
    with open(part) as fh:
        records.extend(json.loads(line) for line in fh if line.strip())

print(json.dumps({
    "parts": len(list(run_dir.glob("part-*.jsonl"))),
    "records": len(records),
    "seqs": [r["seq"] for r in records],
    "types": sorted({r["type"] for r in records}),
    "run_ids": sorted({r["run_id"] for r in records}),
    "terminal": [r for r in records if r["type"] == "status" and r.get("terminal")],
}))
""",
        timeout=240,
    )

    assert found["parts"] >= 1
    assert found["run_ids"] == ["container-2"]
    assert found["seqs"] == sorted(found["seqs"]), "seq is not monotonic across parts"
    assert len(set(found["seqs"])) == len(found["seqs"]), "duplicate seq in the durable record"
    assert {"log", "progress", "status"} <= set(found["types"]), found["types"]
    assert len(found["terminal"]) == 1, "a run must end with exactly one terminal status"
    assert found["terminal"][0]["status"] == "SUCCEEDED"


def test_dbx_model_is_a_name_resolved_by_entry_point(job_image):
    """Not an import path. The job YAML sends `heartbeat`, and the harness asks
    importlib.metadata what that means — which is what lets a model move to its
    own repository without the job file changing."""
    found = probe(
        job_image,
        r"""
import json
from job.loader import installed_models, load_model
handle = load_model("heartbeat", {"seconds": 0.1, "hz": 10})
print(json.dumps({"installed": installed_models(), "spec": handle.spec,
                  "found": sorted(handle.found)}))
""",
    )
    assert found["installed"] == {"heartbeat": "heartbeat:build_model"}
    assert "run" in found["found"]


def test_the_harness_floor_carries_no_model_library(job_image):
    """`job/requirements.txt` is the harness and nothing else.

    tests/deploy/test_heartbeat_job.py greps the file; this checks the
    environment that file actually produced, which is the thing that costs
    money and startup time on a serverless job.
    """
    installed = set(
        probe(
            job_image,
            r"""
import json
from importlib.metadata import distributions
print(json.dumps({"i": sorted({d.metadata["Name"].lower()
                               for d in distributions() if d.metadata["Name"]})}))
""",
        )["i"]
    )
    for library in ("torch", "gurobipy", "scikit-learn", "ortools", "emcee", "numpy", "pandas"):
        assert library not in installed, f"{library} reached a heartbeat job's environment"

    # The app's libraries have no business here either — the job never serves
    # HTTP, it connects out.
    for library in ("fastapi", "uvicorn", "psycopg"):
        assert library not in installed, f"{library} is the app's, not the job's"


def test_an_unknown_model_names_what_is_installed(job_nomodel_image):
    """The error a model author hits most often, in the image that produces it.

    A harness with no model installed is a real deploy state — the job file's
    third dependency line missing, or misspelled. The only useful version of
    this failure names what IS available, and an empty list is itself the
    answer: "nothing is installed" is a different problem from "you typo'd".
    """
    result = run_cmd(
        job_nomodel_image,
        ["python", "-m", "job.main"],
        env={
            "DBX_MODEL": "heartbeat",
            "DBX_RUN_ID": "container-missing",
            "DBX_TELEMETRY_VOLUME": "/tmp/telemetry",
        },
    )
    combined = result.stdout + result.stderr
    assert result.returncode != 0, "a missing model must not be reported as a successful run"
    assert "heartbeat" in combined
    assert "installed" in combined.lower(), (
        f"the failure does not say what is installed:\n{combined[-2000:]}"
    )


def test_the_build_context_matches_what_the_bundle_syncs(job_image):
    """Kept in agreement by hand, so it is asserted rather than trusted.

    A container that saw MORE than the workspace does would pass while the
    deploy failed — the exact direction of error this whole file exists to
    catch. `tests/` is the one deliberate difference and is listed in both.
    """
    bundle = yaml.safe_load((ROOT / "databricks.yml").read_text())
    synced_out = {e.rstrip("/*").rstrip("/") for e in bundle["sync"]["exclude"]}

    ignored = {
        line.strip().rstrip("/*").rstrip("/")
        for line in (ROOT / "tests" / "container" / "Dockerfile.job.dockerignore")
        .read_text()
        .splitlines()
        if line.strip() and not line.startswith("#")
    }

    missing = {e for e in synced_out if e.lstrip("*/") not in {i.lstrip("*/") for i in ignored}}
    assert not missing, (
        f"databricks.yml excludes {sorted(missing)} from the workspace sync but the "
        f"job container still sees them; the container is more generous than the deploy"
    )
