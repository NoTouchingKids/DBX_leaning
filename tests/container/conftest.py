"""Opt-in, and skipped rather than failed when Docker is not there.

These build real images and pull from a registry, which takes minutes and
needs network. Neither belongs in the default `uv run pytest`, so they need
`DBX_CONTAINER_TESTS=1`. A machine with the variable set but no daemon skips
too: no Docker is not a broken repo.

    DBX_CONTAINER_TESTS=1 uv run pytest tests/container -v
"""

from __future__ import annotations

import os

import pytest

from .harness import build, build_base, docker_available

ROOT_MARKERS = pytest.mark.container


def pytest_collection_modifyitems(items):
    for item in items:
        item.add_marker(pytest.mark.container)
        item.add_marker(pytest.mark.slow)


@pytest.fixture(scope="session", autouse=True)
def _requires_docker():
    if os.environ.get("DBX_CONTAINER_TESTS") != "1":
        pytest.skip("set DBX_CONTAINER_TESTS=1 to run the container tests", allow_module_level=True)
    if not docker_available():
        pytest.skip("no Docker daemon", allow_module_level=True)


@pytest.fixture(scope="session")
def base(_requires_docker) -> str:
    return build_base()


@pytest.fixture(scope="session")
def repo_root():
    import pathlib

    return pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def model_image(base, repo_root) -> str:
    """Built from `models/heartbeat/` as its context. Nothing else can travel."""
    return build("model", context=repo_root / "models" / "heartbeat")


@pytest.fixture(scope="session")
def job_image(base, repo_root) -> str:
    return build("job", context=repo_root)


@pytest.fixture(scope="session")
def job_nomodel_image(base, repo_root) -> str:
    return build("job-nomodel", context=repo_root)


@pytest.fixture(scope="session")
def app_image(base, repo_root) -> str:
    """Built from `app/` as its context — what `source_code_path` gives Apps."""
    return build("app", context=repo_root / "app")


@pytest.fixture(scope="session")
def app_noshared_image(base, repo_root) -> str:
    """The same app with `shared/` withheld — today's bug, reproduced.

    Kept so `test_app_container.py` can show that its passing tests would fail
    if the envelope stopped travelling with the app. A green test that cannot
    go red proves nothing.
    """
    return build("app-noshared", context=repo_root / "app")
