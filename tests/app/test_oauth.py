"""The Lakebase credential: a token, not a setting.

A Lakebase instance created with `enable_pg_native_login: false` — the default
— accepts only a short-lived Databricks OAuth token as its Postgres password.
There is no password to put in a secret.

The failure this guards is quiet and delayed. Reading the credential once at
startup and baking it into a connection string works, for about an hour. A
Databricks App runs for up to 24, so what a deployment sees is every Postgres
operation succeeding through the morning and failing thereafter — with nothing
in the logs pointing back at startup, which is where the bug is.
"""

from __future__ import annotations

import httpx
import pytest

from server.config import AppConfig
from server.oauth import DEFAULT_TTL_S, REFRESH_SKEW_S, OAuthTokenProvider, TokenUnavailable
from server.services import ServiceHub

HOST = "https://example.cloud.databricks.com"


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _provider(handler, clock=None, **kwargs) -> OAuthTokenProvider:
    transport = httpx.MockTransport(handler)
    return OAuthTokenProvider(
        HOST,
        "client-id",
        "client-secret",
        http=httpx.AsyncClient(transport=transport),
        now=clock or FakeClock(),
        **kwargs,
    )


def _ok(token="tok-1", expires_in=3600):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"access_token": token, "expires_in": expires_in})

    return handler, calls


async def test_it_exchanges_client_credentials_for_a_token():
    handler, calls = _ok()
    assert await _provider(handler).token() == "tok-1"

    (request,) = calls
    assert str(request.url) == f"{HOST}/oidc/v1/token"
    body = request.content.decode()
    assert "grant_type=client_credentials" in body
    assert "scope=all-apis" in body
    # The secret goes in the Authorization header, not the body.
    assert "client-secret" not in body
    assert request.headers["authorization"].startswith("Basic ")


async def test_the_token_is_cached_rather_than_fetched_per_connection():
    """A round trip on every database operation would put an HTTP call in
    front of a point lookup that exists to be fast."""
    handler, calls = _ok()
    provider = _provider(handler)
    for _ in range(5):
        assert await provider.token() == "tok-1"
    assert len(calls) == 1


async def test_it_refetches_before_the_token_actually_expires():
    """Not after. A request that starts with a valid token and reaches
    Postgres without one is an occasional, unreproducible auth error."""
    clock = FakeClock()
    handler, calls = _ok(expires_in=3600)
    provider = _provider(handler, clock=clock)
    await provider.token()

    clock.t += 3600 - REFRESH_SKEW_S - 1
    await provider.token()
    assert len(calls) == 1, "refetched too eagerly"

    clock.t += 2
    await provider.token()
    assert len(calls) == 2, "did not refetch inside the skew window"


async def test_a_ttl_shorter_than_the_skew_still_caches():
    """`expires_in - skew` would be negative, refetching on every connection —
    turning a cache into a per-operation HTTP call."""
    clock = FakeClock()
    handler, calls = _ok(expires_in=60)
    provider = _provider(handler, clock=clock)
    await provider.token()
    clock.t += 20
    await provider.token()
    assert len(calls) == 1


async def test_a_response_with_no_expiry_is_assumed_short():
    """Guessing low costs a round trip; guessing high costs failed
    connections."""
    clock = FakeClock()

    def handler(request):
        return httpx.Response(200, json={"access_token": "tok"})

    provider = _provider(handler, clock=clock)
    await provider.token()
    assert provider._expires_at <= clock.t + DEFAULT_TTL_S


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (httpx.Response(401, text='{"error":"invalid_client"}'), "401"),
        (httpx.Response(200, json={"token_type": "Bearer"}), "no access_token"),
    ],
)
async def test_a_failed_exchange_raises_rather_than_returning_nothing(response, expected):
    """An empty password reaches Postgres as a password mismatch, which sends
    whoever reads the log looking for the wrong problem."""
    provider = _provider(lambda request: response)
    with pytest.raises(TokenUnavailable) as exc:
        await provider.token()
    assert expected in str(exc.value)


async def test_an_unreachable_endpoint_raises_the_same_way():
    def handler(request):
        raise httpx.ConnectError("no route to host")

    with pytest.raises(TokenUnavailable, match="could not reach"):
        await _provider(handler).token()


# --- how the app decides which credential to use --------------------------


def _config(**env) -> AppConfig:
    return AppConfig.from_env({"DBX_LAKEBASE_HOST": "pg.example.com", **env})


def test_client_credentials_produce_a_provider():
    cfg = _config(
        DATABRICKS_HOST=HOST,
        DATABRICKS_CLIENT_ID="id",
        DATABRICKS_CLIENT_SECRET="secret",
    )
    assert cfg.has_client_credentials
    assert ServiceHub._lakebase_password(cfg) is not None


def test_without_them_the_dsn_is_used_as_is():
    """The local dev stack, and an instance with native login enabled."""
    cfg = _config(DBX_LAKEBASE_PASSWORD="static")
    assert not cfg.has_client_credentials
    assert ServiceHub._lakebase_password(cfg) is None


def test_a_client_id_with_no_host_is_not_enough():
    """The token endpoint is built from the workspace host; without one there
    is nowhere to send the exchange."""
    cfg = _config(DATABRICKS_CLIENT_ID="id", DATABRICKS_CLIENT_SECRET="secret")
    assert not cfg.has_client_credentials


def test_the_env_names_can_be_overridden():
    """Databricks Apps injecting DATABRICKS_CLIENT_ID/SECRET is a platform
    behaviour this repo has not verified against a real workspace, so it is
    the default rather than the only option."""
    cfg = _config(
        DATABRICKS_HOST=HOST,
        DBX_OAUTH_CLIENT_ID="explicit-id",
        DBX_OAUTH_CLIENT_SECRET="explicit-secret",
    )
    assert cfg.oauth_client_id == "explicit-id"
    assert cfg.oauth_client_secret == "explicit-secret"
