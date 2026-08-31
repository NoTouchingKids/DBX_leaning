"""The deployed app, with only `app/` present.

`resources/app.yml` hands Databricks Apps `../app` as its `source_code_path`
and nothing above it travels. An app can also be deployed with no bundle at
all — the Apps UI, or `databricks apps deploy --source-code-path ...` — which
sees only tracked files.

This file exists because of a specific failure. Deleting `app/shared/` as
apparent duplication left all 296 tests green and broke the deployed app:
pytest has the repo root on its `sys.path` and a Databricks App does not.
`tests/deploy/test_app_is_self_contained.py` reads the imports statically,
which is fast and catches the same class of thing. This one actually starts the
app, and `test_the_suite_would_have_caught_the_bug_that_started_this` builds
the broken variant to show the check can still go red.
"""

from __future__ import annotations

from .harness import probe, run_cmd


def test_the_app_imports_with_nothing_above_it(app_image):
    """`shared` is the one that matters. It lives under `app/` precisely so
    this works, and every other test here would pass trivially without it."""
    found = probe(
        app_image,
        r"""
import json
out = {}
for name in ("server", "server.main", "shared", "shared.envelope", "shared.rpc"):
    try:
        __import__(name)
        out[name] = True
    except Exception as exc:
        out[name] = f"{type(exc).__name__}: {exc}"
print(json.dumps(out))
""",
    )
    for name, result in found.items():
        assert result is True, f"{name} did not import in the deployed app: {result}"


def test_the_app_does_not_need_the_harness_or_any_model(app_image):
    """The boundary, from the app's side.

    The app triggers jobs through the Jobs API and observes them over a socket;
    it never imports a model, which is what keeps gurobipy and torch out of a
    web app's environment. If this ever starts passing by accident — because
    something vendored the harness in — that is worth knowing.
    """
    found = probe(
        app_image,
        r"""
import json
out = {}
for name in ("job", "heartbeat"):
    try:
        __import__(name)
        out[name] = True
    except Exception as exc:
        out[name] = type(exc).__name__
print(json.dumps(out))
""",
    )
    assert found == {"job": "ModuleNotFoundError", "heartbeat": "ModuleNotFoundError"}, found


def test_the_command_in_app_yaml_actually_serves(app_image):
    """Not `import server.main` — the command Databricks Apps runs, verbatim.

    Databricks assigns the port and marks a deployment failed if its health
    check cannot connect, so "the module imports" is a weaker claim than it
    looks. `--network none` still gives loopback, which is all this needs.
    """
    found = probe(
        app_image,
        r"""
import json, os, subprocess, sys, time, urllib.request

proc = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "server.main:app", "--host", "127.0.0.1",
     "--port", "8123"],
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    env=dict(os.environ, DBX_FRONTEND_DIST="dist"),
)
body, status, err = None, None, None
try:
    for _ in range(120):
        if proc.poll() is not None:
            err = "uvicorn exited: " + (proc.stdout.read() or "")[-1500:]
            break
        try:
            with urllib.request.urlopen("http://127.0.0.1:8123/healthz", timeout=2) as r:
                status, body = r.status, r.read().decode()
            break
        except Exception:
            time.sleep(0.25)
finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()

print(json.dumps({"status": status, "body": (body or "")[:1500], "error": err}))
""",
        timeout=240,
    )

    assert found["error"] is None, found["error"]
    assert found["status"] == 200, found
    # Degraded is the expected posture here: no Lakebase, no warehouse, no
    # volume in a container. What matters is that it SAYS so rather than
    # failing to start — an unconfigured deploy is supported, not broken.
    assert found["body"], "healthz returned an empty body"


def test_healthz_reports_the_missing_services_rather_than_dying(app_image):
    """The unconfigured deploy is a supported state, and this is how it reads.

    A container has no Lakebase, no warehouse and no volume. The app is
    designed to start anyway and report what is degraded — if it ever starts
    refusing to boot without them, a first deploy becomes a chicken-and-egg.
    """
    found = probe(
        app_image,
        r"""
import json, os, subprocess, sys, time, urllib.request
proc = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "server.main:app", "--host", "127.0.0.1",
     "--port", "8124"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    env=dict(os.environ, DBX_FRONTEND_DIST="dist"))
payload = {}
try:
    for _ in range(120):
        try:
            with urllib.request.urlopen("http://127.0.0.1:8124/healthz", timeout=2) as r:
                payload = json.loads(r.read().decode())
            break
        except Exception:
            time.sleep(0.25)
finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()
print(json.dumps({"healthz": payload}))
""",
        timeout=240,
    )["healthz"]

    assert found, "healthz gave nothing back"
    # Not asserting exact keys — that is app/server/routes/meta.py's business
    # and this file should not pin its shape. What is asserted is that it
    # answered at all with the whole world absent.
    assert isinstance(found, dict)


def test_the_suite_would_have_caught_the_bug_that_started_this(app_noshared_image):
    """A mutation check: withhold `shared/` and the app must fail to start.

    Without this, everything above proves the app works and nothing proves the
    check can go red. The point is not that the app breaks — it obviously does
    — but that it breaks HERE, in an image built the way Databricks builds it,
    while the ordinary suite stays green. That asymmetry is the whole
    justification for container tests.
    """
    result = run_cmd(app_noshared_image, ["python", "-c", "import server.main"])

    assert result.returncode != 0, (
        "the app imported without shared/, so this image is not reproducing the "
        "failure and the tests above are not load-bearing"
    )
    combined = result.stdout + result.stderr
    assert "shared" in combined, combined[-1500:]
    assert "ModuleNotFoundError" in combined or "ImportError" in combined, combined[-1500:]
