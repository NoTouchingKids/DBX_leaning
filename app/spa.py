"""Serving the built React bundle from FastAPI.

Databricks Apps runs a Python process and nothing else — there is no Node
runtime at deploy time — so the same app that serves ``/api`` is what serves
``index.html`` and the hashed asset files Vite emits. There is no separate
static host to point at.

Two things this module exists to get right:

**Ordering.** The SPA fallback is a catch-all (``/{full_path:path}``). Starlette
matches routes in registration order and returns the first match, so the
fallback must be registered *after* every API router or it swallows ``/api``,
``/ws`` and ``/healthz``. :func:`mount_spa` is therefore called from
``create_app`` as the last thing it does, and it *also* refuses reserved
prefixes itself — belt and braces, because "the router order changed" is a
silent failure otherwise: ``/api/runs`` would start returning HTML with a 200
and the client would fail somewhere far away, in a JSON parser.

**A missing bundle degrades, loudly.** The dist directory does not exist in a
source checkout and never exists during tests. That must not stop the API from
starting, and it must not look like a routing bug either: the fallback is still
registered, but it answers 503 with a message that says the frontend has not
been built, instead of a bare 404 someone will spend an afternoon chasing.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

log = logging.getLogger(__name__)

__all__ = ["RESERVED_PREFIXES", "NO_BUNDLE", "resolve_dist", "mount_spa"]

#: ``app/`` lives one level below the repo root, which is what a relative
#: ``frontend/dist`` is relative *to* — not the process's cwd, which on
#: Databricks Apps is not something this code chose.
_REPO_ROOT = Path(__file__).resolve().parents[1]

#: First path segments the SPA fallback must never answer for. Everything here
#: is owned by an API router (or by FastAPI's own docs routes); a request for
#: one of these that reaches the fallback is a 404, not a client route.
RESERVED_PREFIXES = frozenset(
    {"api", "ws", "healthz", "docs", "redoc", "openapi.json"}
)

NO_BUNDLE = (
    "the frontend bundle has not been built, so there is nothing to serve at "
    "this path. Build it (`pnpm build` in frontend/) or point "
    "DBX_FRONTEND_DIST at the dist directory. The API is unaffected."
)


def resolve_dist(dist_dir: str | Path) -> Path:
    """Absolute path to the built bundle. Relative paths are repo-root
    relative, deliberately: cwd is not ours to depend on."""
    path = Path(dist_dir)
    if not path.is_absolute():
        path = _REPO_ROOT / path
    return path.resolve()


def _file_under(root: Path, relative: str) -> Path | None:
    """An existing regular file inside ``root``, or None.

    ``resolve()`` then a containment check, so ``../../etc/passwd`` and a
    symlink pointing out of the bundle both fail closed.
    """
    if not relative:
        return None
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        return None
    return candidate


def _index_response(index: Path) -> FileResponse:
    # index.html must never be cached: it is the one file that names the
    # current hashed bundle, and a stale copy points at assets that a redeploy
    # has already deleted. The hashed assets themselves are immutable and
    # StaticFiles' own ETag/Last-Modified handling is fine for them.
    return FileResponse(index, headers={"Cache-Control": "no-cache"})


def mount_spa(app: FastAPI, dist_dir: str | Path) -> bool:
    """Register static assets and the client-route fallback. Call this last.

    Returns whether a built bundle was found. Registration happens either way,
    so a non-API path always gets a clear answer rather than a bare 404.
    """
    dist = resolve_dist(dist_dir)
    index = dist / "index.html"
    built = index.is_file()

    if built:
        assets = dist / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")
        else:
            # A bundle with no assets/ is odd but not fatal — the catch-all
            # serves any file that is actually in dist/.
            log.warning("frontend bundle at %s has no assets/ directory", dist)
        log.info("serving the frontend bundle from %s", dist)
    else:
        # Once, at startup, and loudly enough to be findable in the app log.
        log.warning("no frontend bundle at %s: %s", dist, NO_BUNDLE)

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str) -> Response:
        """Any non-API GET returns index.html, so React Router's client-side
        routes survive a hard refresh (and a bookmark, and a shared link)."""
        first = full_path.split("/", 1)[0]
        if first in RESERVED_PREFIXES:
            # Registered after the API routers, so reaching here means no API
            # route matched. Say that, rather than handing back an HTML page
            # with a 200 that fails later in a JSON parser.
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, f"no such endpoint: /{full_path}"
            )
        if not built:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, NO_BUNDLE)

        # A real file in the bundle (favicon.ico, manifest.webmanifest, ...)
        # wins over the fallback; everything else is a client route.
        found = _file_under(dist, full_path)
        return FileResponse(found) if found is not None else _index_response(index)

    return built
