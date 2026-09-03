"""`M2MTokenProvider`: the job side of the same exchange `app/server/oauth.py`
does for the app's Lakebase credential — `client_credentials` against the
workspace's own `/oidc/v1/token`, over `httpx`, no SDK.

Deliberately close to `tests/app/test_oauth.py` in shape. The two providers
mirror each other on purpose (sync here because the job's socket thread owns
no event loop; async there because the app is asyncio throughout), and a test
suite that looked nothing like the one for the class it was copied from would
be the first sign the copy had drifted.
"""

from __future__ import annotations

import httpx
import pytest

from job.auth import DEFAULT_TTL_S, REFRESH_SKEW_S, M2MTokenProvider, TokenUnavailable

HOST = "https://example.cloud.databricks.com"


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _provider(handler, clock=None, **kwargs) -> M2MTokenProvider:
    transport = httpx.MockTransport(handler)
    return M2MTokenProvider(
        HOST,
        "client-id",
        "client-secret",
        http=httpx.Client(transport=transport),
        now=clock or FakeClock(),
        **kwargs,
    )


def _ok(token="tok-1", expires_in=3600):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"access_token": token, "expires_in": expires_in})

    return handler, calls


def test_it_exchanges_client_credentials_for_a_token():
    handler, calls = _ok()
    assert _provider(handler).token() == "tok-1"

    (request,) = calls
    assert str(request.url) == f"{HOST}/oidc/v1/token"
    body = request.content.decode()
    assert "grant_type=client_credentials" in body
    assert "scope=all-apis" in body
    # The secret goes in the Authorization header, not the body.
    assert "client-secret" not in body
    assert request.headers["authorization"].startswith("Basic ")


def test_a_host_with_a_scheme_already_on_it_is_not_doubled():
    """`workspace_host` arrives as `https://...` from `JobConfig`; the URL
    must not come out `https://https://...`."""
    handler, calls = _ok()
    _provider(handler).token()
    assert str(calls[0].url).startswith("https://example.cloud.databricks.com/")
    assert "https://https://" not in str(calls[0].url)


def test_the_token_is_cached_rather_than_fetched_per_connection():
    """A round trip on every reconnect would put an HTTP call in front of
    what is supposed to be a point lookup."""
    handler, calls = _ok()
    provider = _provider(handler)
    for _ in range(5):
        assert provider.token() == "tok-1"
    assert len(calls) == 1


def test_it_refetches_before_the_token_actually_expires():
    """Not after. A reconnect that presents a token which expired forty
    minutes ago is an occasional, unreproducible 302."""
    clock = FakeClock()
    handler, calls = _ok(expires_in=3600)
    provider = _provider(handler, clock=clock)
    provider.token()

    clock.t += 3600 - REFRESH_SKEW_S - 1
    provider.token()
    assert len(calls) == 1, "refetched too eagerly"

    clock.t += 2
    provider.token()
    assert len(calls) == 2, "did not refetch inside the skew window"


def test_a_ttl_shorter_than_the_skew_still_caches():
    """`expires_in - skew` would be negative, refetching on every connection
    attempt — turning a cache into a per-attempt HTTP call."""
    clock = FakeClock()
    handler, calls = _ok(expires_in=60)
    provider = _provider(handler, clock=clock)
    provider.token()
    clock.t += 20
    provider.token()
    assert len(calls) == 1


def test_a_response_with_no_expiry_is_assumed_short():
    """Guessing low costs a round trip; guessing high costs failed
    connections."""
    clock = FakeClock()

    def handler(request):
        return httpx.Response(200, json={"access_token": "tok"})

    provider = _provider(handler, clock=clock)
    provider.token()
    assert provider._expires_at <= clock.t + DEFAULT_TTL_S


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (httpx.Response(401, text='{"error":"invalid_client"}'), "401"),
        (httpx.Response(200, json={"token_type": "Bearer"}), "no access_token"),
    ],
)
def test_a_failed_exchange_raises_rather_than_returning_nothing(response, expected):
    """An empty header reaches the app's ingress as an unauthenticated
    upgrade, which sends whoever reads the log looking for the wrong problem —
    `auth_headers` is what turns this into "running unobserved" instead."""
    provider = _provider(lambda request: response)
    with pytest.raises(TokenUnavailable) as exc:
        provider.token()
    assert expected in str(exc.value)


def test_an_unreachable_endpoint_raises_the_same_way():
    def handler(request):
        raise httpx.ConnectError("no route to host")

    with pytest.raises(TokenUnavailable, match="could not reach"):
        _provider(handler).token()
