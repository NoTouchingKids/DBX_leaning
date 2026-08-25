"""Serving the built SPA, and not serving it when it is not there.

The catch-all that makes React Router survive a hard refresh is the one route
that can shadow every other route in the app, so most of what is checked here
is that it does not.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.spa import RESERVED_PREFIXES, resolve_dist


@pytest.fixture
def dist(tmp_path):
    """A minimal Vite-shaped build output."""
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><div id=root></div>")
    (root / "assets" / "index-abc123.js").write_text("console.log(1)")
    (root / "favicon.ico").write_text("not really an icon")
    return root


@pytest.fixture
def built(app_and_hub, config, dist):
    app, hub = app_and_hub(config(frontend_dist=str(dist)))
    return app, hub


# --- ordering: the catch-all must not swallow the API -----------------------


def test_api_routes_still_answer_with_the_catch_all_registered(built):
    """Registered after the routers, so every API route still wins. If this
    breaks, /api/models returns index.html with a 200 and the failure surfaces
    in the client's JSON parser, three layers away from the cause."""
    app, _ = built
    with TestClient(app) as client:
        health = client.get("/healthz")
        models = client.get("/api/models")
        schema = client.get("/api/schema")
        whoami = client.get("/api/whoami")

    for resp in (health, models, schema, whoami):
        assert resp.status_code == 200, resp.request.url
        assert resp.headers["content-type"].startswith("application/json")
    assert "status" in health.json()


def test_an_unknown_api_path_is_a_404_not_the_index(built):
    """A mistyped API path must stay a 404. Answering it with the SPA turns a
    typo into a 200 full of HTML."""
    app, _ = built
    with TestClient(app) as client:
        resp = client.get("/api/nope")
    assert resp.status_code == 404
    assert "no such endpoint" in resp.json()["detail"]


def test_a_websocket_path_fetched_over_http_is_not_the_index(built):
    app, _ = built
    with TestClient(app) as client:
        resp = client.get("/ws/job/r1")
    assert resp.status_code == 404
    assert "text/html" not in resp.headers["content-type"]


def _top_level_prefixes(app) -> set[str]:
    """Every first path segment the app routes, catch-all excluded.

    Included routers are wrapped in this FastAPI version, so this walks
    through the wrapper rather than reading the top level only.
    """
    prefixes = set()
    stack = list(app.router.routes)
    while stack:
        route = stack.pop()
        inner = getattr(route, "original_router", None)
        if inner is not None:
            stack.extend(inner.routes)
            continue
        path = getattr(route, "path", "")
        if path and "{full_path" not in path:
            prefixes.add(path.lstrip("/").split("/")[0])
    return prefixes


def test_the_reserved_list_covers_every_prefix_the_app_routes(built):
    """The reserved list is only correct if it matches what is mounted. A new
    top-level prefix added without updating it would be shadowed by the SPA
    fallback for any path the router itself does not match exactly."""
    app, _ = built
    assert _top_level_prefixes(app) <= RESERVED_PREFIXES | {"assets"}


# --- the fallback itself ----------------------------------------------------


def test_a_deep_client_route_returns_the_index(built):
    """A hard refresh on /runs/run-abc/mcmc has to work: the server has never
    heard of that path, React Router has."""
    app, _ = built
    with TestClient(app) as client:
        for path in ("/", "/runs", "/runs/run-abc123", "/models/mcmc/settings"):
            resp = client.get(path)
            assert resp.status_code == 200, path
            assert "<div id=root>" in resp.text, path
            assert resp.headers["content-type"].startswith("text/html")


def test_the_index_is_not_cached(built):
    """index.html names the current hashed bundle; a cached copy points at
    assets a redeploy has already deleted."""
    app, _ = built
    with TestClient(app) as client:
        resp = client.get("/runs")
    assert resp.headers["cache-control"] == "no-cache"


def test_hashed_assets_are_served_from_the_mount(built):
    app, _ = built
    with TestClient(app) as client:
        resp = client.get("/assets/index-abc123.js")
    assert resp.status_code == 200
    assert resp.text == "console.log(1)"


def test_a_real_file_in_the_bundle_wins_over_the_fallback(built):
    app, _ = built
    with TestClient(app) as client:
        resp = client.get("/favicon.ico")
    assert resp.text == "not really an icon"


def test_the_fallback_does_not_serve_files_outside_the_bundle(built, dist):
    (dist.parent / "secret.txt").write_text("nope")
    app, _ = built
    with TestClient(app) as client:
        attempts = [
            client.get("/../secret.txt"),
            client.get("/%2e%2e/secret.txt"),
            client.get("/assets/../../secret.txt"),
        ]
    for resp in attempts:
        assert "nope" not in resp.text


# --- a missing bundle degrades rather than crashing -------------------------


def test_a_missing_dist_does_not_stop_the_app_starting(app_and_hub, config, tmp_path):
    """The bundle does not exist in a source checkout and never exists in
    tests. That is a degraded frontend, not a broken API."""
    app, _ = app_and_hub(config(frontend_dist=str(tmp_path / "never-built")))
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/api/models").status_code == 200


def test_a_missing_dist_says_so_instead_of_a_bare_404(app_and_hub, config, tmp_path):
    app, _ = app_and_hub(config(frontend_dist=str(tmp_path / "never-built")))
    with TestClient(app) as client:
        resp = client.get("/runs/run-abc123")
    assert resp.status_code == 503
    assert "has not been built" in resp.json()["detail"]


def test_a_missing_dist_is_logged_once_at_startup(app_and_hub, config, tmp_path, caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="app.spa"):
        app_and_hub(config(frontend_dist=str(tmp_path / "never-built")))
    warnings = [r for r in caplog.records if "no frontend bundle" in r.message]
    assert len(warnings) == 1


def test_api_paths_are_still_404_not_503_without_a_bundle(app_and_hub, config, tmp_path):
    """The missing-bundle answer must not leak onto the API surface, or a
    genuine 404 starts reading like a deployment problem."""
    app, _ = app_and_hub(config(frontend_dist=str(tmp_path / "never-built")))
    with TestClient(app) as client:
        assert client.get("/api/nope").status_code == 404


def test_the_default_dist_is_repo_relative_not_cwd_relative(config):
    """Databricks Apps does not promise a working directory; resolving
    against the repo root means the path does not depend on one."""
    resolved = resolve_dist(config().frontend_dist)
    assert resolved.is_absolute()
    # `dist/` at the repo root, which is also the app root a deploy hands to
    # Databricks Apps — not `frontend/dist`, which is the client SOURCE tree.
    assert resolved.parts[-1] == "dist"
    assert resolved.parts[-2] != "frontend"
