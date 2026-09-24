from __future__ import annotations

import pytest

from server.config import AppConfig
from server.main import create_app
from server.services import ServiceHub


@pytest.fixture
def config():
    def _make(**kw):
        base = dict(
            catalog="main",
            schema="dbx_leaning",
            sse_keepalive_s=0.05,
            reconcile_on_startup=False,
        )
        base.update(kw)
        return AppConfig(**base)

    return _make


@pytest.fixture
def app_and_hub(config):
    """An app with its hub built directly, so tests can reach in.

    Mirrors what lifespan does, minus the lifespan — which keeps these tests
    free of any startup I/O.
    """

    def _make(cfg=None, **hub_overrides):
        cfg = cfg or config()
        application = create_app(cfg)
        hub = ServiceHub(cfg)
        for key, value in hub_overrides.items():
            setattr(hub, key, value)
        application.state.hub = hub
        return application, hub

    return _make


class FakeHttp:
    """Stands in for httpx.AsyncClient, recording what was sent."""

    def __init__(self, response=None, status_code=200):
        self.requests: list[dict] = []
        self._response = response or {}
        self.status_code = status_code

    async def post(self, url, json=None, headers=None):
        self.requests.append({"url": url, "json": json, "headers": headers})
        return _Response(self._response, self.status_code)

    async def get(self, url, params=None, headers=None):
        self.requests.append({"url": url, "params": params, "headers": headers})
        return _Response(self._response, self.status_code)

    async def aclose(self): ...


class _Response:
    def __init__(self, payload, status_code):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload


def statement_response(columns: list[str], rows: list[list]) -> dict:
    return {
        "status": {"state": "SUCCEEDED"},
        "manifest": {"schema": {"columns": [{"name": c} for c in columns]}},
        "result": {"data_array": rows},
    }
