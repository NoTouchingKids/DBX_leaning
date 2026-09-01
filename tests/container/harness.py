"""Build and run the test containers.

Kept separate from the tests so the same code can be driven by hand:

    uv run python -m tests.container.harness build
    uv run python -m tests.container.harness shell model

**Why containers at all.** The rest of the suite proves the model contract by
importing things in a subprocess — and a subprocess inherits the repo root on
`sys.path`, so "a model needs nothing from the platform" is asserted in an
environment where the whole platform happens to be importable. That is the
same blindness that let `app/shared/` be deleted with 296 tests green.

A container fixes it by construction rather than by discipline: the model image
is built with `models/<name>/` as its BUILD CONTEXT, so the Dockerfile cannot
copy the repo in even if someone edits it to try.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
HERE = pathlib.Path(__file__).resolve().parent

#: NOT `python:3.11-slim` from Docker Hub, and the reason is an egress policy
#: rather than a preference. Hub's manifest fetch succeeds and its blob CDN
#: then 403s:
#:
#:   production.cloudfront.docker.com/.../data: 403 Forbidden
#:
#: which reads like a broken pull rather than a blocked host. Probed
#: 2026-08-31: `mirror.gcr.io` and `mcr.microsoft.com` both serve manifests AND
#: blobs; `quay.io` and the Hub CDN do not resolve at all. Two work, so:
#:
#:   mirror.gcr.io/library/python:3.11-slim   the real python:3.11-slim, ~150MB
#:   mcr.microsoft.com/devcontainers/python:3.11   verified, but ~1.5GB
#:
#: The mirror wins on size and on being byte-identical to upstream. Override
#: this on a machine whose egress differs — it is a fact about one sandbox, not
#: about anything under test.
PYTHON_IMAGE = "mirror.gcr.io/library/python:3.11-slim"

BASE_TAG = "dbx-test-base:latest"

#: The proxy's CA, if this machine has one. Absent on an ordinary machine, in
#: which case the base image is built without the certificate layer.
PROXY_CA = pathlib.Path("/root/.ccr/ca-bundle.crt")


#: Where a detached daemon writes. Not a temp file: the log outlives the
#: process that started it, which is the whole point.
DAEMON_LOG = pathlib.Path("/tmp/dbx-dockerd.log")


class DockerUnavailable(RuntimeError):
    """Raised when there is no daemon to talk to. Tests skip on this rather
    than failing: a machine without Docker is not a broken repo."""


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return (
            subprocess.run(
                ["docker", "info"], capture_output=True, timeout=20, check=False
            ).returncode
            == 0
        )
    except (OSError, subprocess.TimeoutExpired):
        return False


def _run(cmd: list[str], *, timeout: int = 900) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)


def start_daemon(*, wait_s: int = 30) -> bool:
    """Start `dockerd` DETACHED, and wait for its socket. Returns success.

    **Detached matters, and the reason is a mistake worth not repeating.** A
    daemon started as a tracked background command never exits, so the harness
    that started it goes on reporting a running task indefinitely — one such
    daemon sat there for seven hours looking like a seven-hour test. `dockerd`
    is not a long test; it is not a test at all. `start_new_session=True` is
    what puts it outside the caller's process group so nothing waits on it.

    Not called from a fixture. Starting a system daemon as a side effect of
    `pytest` is more than a test should help itself to, so the tests skip with
    a message pointing here and this stays an explicit step:

        uv run python -m tests.container.harness daemon
    """
    if docker_available():
        return True

    # The daemon does its own registry pulls and reads none of this process's
    # environment, so a proxied machine needs the proxy in ITS config or every
    # pull dies on the first blob. Written only when absent, and only when
    # there is in fact a proxy to point at.
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    config = pathlib.Path("/etc/docker/daemon.json")
    if proxy and not config.exists():
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(
            json.dumps(
                {
                    "proxies": {
                        "http-proxy": proxy,
                        "https-proxy": proxy,
                        "no-proxy": "localhost,127.0.0.1,::1",
                    }
                },
                indent=2,
            )
        )
        print(f"wrote {config} pointing at {proxy}")

    log = DAEMON_LOG.open("a")
    subprocess.Popen(
        ["dockerd", "--iptables=false"],
        stdout=log,
        stderr=log,
        start_new_session=True,  # see the docstring — do NOT drop this
    )
    for _ in range(wait_s):
        if docker_available():
            return True
        time.sleep(1)
    return False


def stop_daemon() -> None:
    """Stop a daemon `start_daemon` started. Images survive in /var/lib/docker."""
    subprocess.run(["pkill", "-f", "^dockerd"], check=False)
    subprocess.run(["pkill", "-f", "containerd --config"], check=False)


def build_base(*, quiet: bool = True) -> str:
    """The image the three role images share. Carries no repo code at all."""
    with tempfile.TemporaryDirectory() as tmp:
        ctx = pathlib.Path(tmp)
        if PROXY_CA.exists():
            shutil.copy(PROXY_CA, ctx / "ca-bundle.crt")
            dockerfile = HERE / "base" / "Dockerfile"
        else:
            dockerfile = HERE / "base" / "Dockerfile.nocert"
        cmd = [
            "docker",
            "build",
            "--network",
            "host",
            "--build-arg",
            f"PYTHON_IMAGE={PYTHON_IMAGE}",
            "-f",
            str(dockerfile),
            "-t",
            BASE_TAG,
            str(ctx),
        ]
        if quiet:
            cmd.insert(2, "--quiet")
        result = _run(cmd)
    if result.returncode != 0:
        raise DockerUnavailable(f"base image build failed:\n{result.stderr[-3000:]}")
    return BASE_TAG


def build(role: str, *, context: pathlib.Path, quiet: bool = True) -> str:
    """Build one role image. `context` is the enforcement — see the module
    docstring and the header of each Dockerfile."""
    tag = f"dbx-test-{role}:latest"
    cmd = [
        "docker",
        "build",
        "--network",
        "host",
        "--build-arg",
        f"BASE={BASE_TAG}",
        "-f",
        str(HERE / f"Dockerfile.{role}"),
        "-t",
        tag,
        str(context),
    ]
    if quiet:
        cmd.insert(2, "--quiet")
    result = _run(cmd)
    if result.returncode != 0:
        raise AssertionError(
            f"building {role} failed:\n{result.stdout[-2000:]}\n{result.stderr[-3000:]}"
        )
    return tag


def run(
    tag: str,
    script: str,
    *,
    env: dict[str, str] | None = None,
    timeout: int = 180,
    network: str = "none",
) -> subprocess.CompletedProcess[str]:
    """Run a Python snippet in a role image.

    `network=none` by default and deliberately: a model that quietly reached the
    internet at run time would be a real finding, and Free Edition's egress
    restrictions mean it would fail on the workspace rather than here. Anything
    that has to install at run time passes `network="host"` explicitly.
    """
    cmd = ["docker", "run", "--rm", "--network", network]
    for key, value in (env or {}).items():
        cmd += ["-e", f"{key}={value}"]
    cmd += [tag, "python", "-c", script]
    return _run(cmd, timeout=timeout)


def run_cmd(
    tag: str,
    argv: list[str],
    *,
    env: dict[str, str] | None = None,
    timeout: int = 180,
    network: str = "none",
) -> subprocess.CompletedProcess[str]:
    """Run a real command in a role image — `python -m job.main`, `uvicorn`.

    Separate from `run` because the interesting job and app tests are about the
    ENTRYPOINT, not about a snippet: what a Databricks task and a Databricks App
    actually execute, run against the same installed distributions.
    """
    cmd = ["docker", "run", "--rm", "--network", network]
    for key, value in (env or {}).items():
        cmd += ["-e", f"{key}={value}"]
    return _run(cmd + [tag] + argv, timeout=timeout)


def probe(tag: str, script: str, **kwargs) -> dict:
    """Run a snippet that prints one JSON object, and return it.

    Tests assert on structured facts rather than scraping stdout, so a failure
    reports what the container actually found.
    """
    result = run(tag, script, **kwargs)
    line = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    try:
        return json.loads(line)
    except json.JSONDecodeError as exc:  # pragma: no cover - diagnostic path
        raise AssertionError(
            f"probe produced no JSON (exit {result.returncode})\n"
            f"--- stdout ---\n{result.stdout[-2000:]}\n"
            f"--- stderr ---\n{result.stderr[-2000:]}"
        ) from exc


IMPORTABLE = """
import importlib, json
out = {}
for name in %(names)s:
    try:
        importlib.import_module(name)
        out[name] = True
    except Exception as exc:
        out[name] = type(exc).__name__
print(json.dumps(out))
"""


def importable(tag: str, names: list[str]) -> dict[str, object]:
    """`{name: True}` if it imported, `{name: "ModuleNotFoundError"}` if not."""
    return probe(tag, IMPORTABLE % {"names": repr(names)})


def _cli() -> int:
    action = sys.argv[1] if len(sys.argv) > 1 else "build"
    if action == "daemon":
        ok = start_daemon()
        print("daemon up" if ok else f"daemon did not start; see {DAEMON_LOG}")
        return 0 if ok else 1
    if action == "stop":
        stop_daemon()
        print("daemon stopped; images kept")
        return 0
    if not docker_available():
        print(
            "no docker daemon — start one with:\n  uv run python -m tests.container.harness daemon",
            file=sys.stderr,
        )
        return 1
    build_base(quiet=False)
    contexts = {
        "model": ROOT / "models" / "heartbeat",
        "job": ROOT,
        "job-nomodel": ROOT,
        "app": ROOT / "app",
    }
    if action == "build":
        for role, ctx in contexts.items():
            print(f"building {role} from {ctx.relative_to(ROOT) or '.'} ...")
            print(" ", build(role, context=ctx, quiet=False))
        return 0
    if action == "shell":
        role = sys.argv[2]
        build(role, context=contexts[role], quiet=False)
        return subprocess.call(
            ["docker", "run", "--rm", "-it", "--network", "none", f"dbx-test-{role}:latest", "bash"]
        )
    print(f"unknown action {action!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_cli())
