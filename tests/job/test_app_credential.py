"""The Databricks identity a job presents to reach the app.

Two authentications happen on one request, and there is only one
`Authorization` header:

1. The **Databricks Apps proxy** in front of the app lets nothing through
   without a Databricks OAuth token, and reads `Authorization: Bearer`.
2. The **app's own** ingress check authenticates the job *process* with the
   shared `DBX_APP_TOKEN` the app hands each run at trigger time. That is not
   a Databricks identity and the proxy rejects it.

The job used to put the shared secret in `Authorization`, which meant the
proxy refused the handshake before the app saw anything — a run that went
unobserved with nothing in the app's log to say why. So the secret moved to
its own header and the OAuth token took that one.
"""

from __future__ import annotations

import pytest

from job.auth import APP_TOKEN_HEADER, AppCredential, ingress_headers


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


class TestWhichHeaderCarriesWhat:
    async def test_the_shared_secret_never_goes_in_authorization(self):
        """The whole bug. `Authorization` is the proxy's."""
        headers = await ingress_headers("shared-secret", None)

        assert headers[APP_TOKEN_HEADER] == "shared-secret"
        assert "Authorization" not in headers

    async def test_the_oauth_token_goes_in_authorization(self):
        credential = AppCredential(env={"DBX_APP_OAUTH_TOKEN": "oauth"})
        headers = await ingress_headers("shared-secret", credential)

        assert headers["Authorization"] == "Bearer oauth"
        assert headers[APP_TOKEN_HEADER] == "shared-secret"

    async def test_no_credential_and_no_secret_is_an_empty_set(self):
        """The local dev stack: no proxy to satisfy, no secret configured."""
        assert await ingress_headers(None, None) == {}

    async def test_a_credential_that_finds_nothing_still_sends_the_secret(self):
        """A job with no Databricks identity is not a broken job — it just
        cannot get through a proxy. Against a directly reachable app it works."""
        credential = AppCredential(env={})
        headers = await ingress_headers("shared-secret", credential)

        assert headers == {APP_TOKEN_HEADER: "shared-secret"}


class TestWhereTheIdentityComesFrom:
    async def test_an_explicit_token_is_used_as_is(self):
        credential = AppCredential(env={"DBX_APP_OAUTH_TOKEN": "handed-in"})
        assert await credential.token() == "handed-in"
        assert credential.source == "DBX_APP_OAUTH_TOKEN"

    async def test_a_personal_access_token_is_used_when_there_is_one(self):
        credential = AppCredential(env={"DATABRICKS_TOKEN": "pat"})
        assert await credential.token() == "pat"

    async def test_client_credentials_are_exchanged_at_the_oidc_endpoint(self, monkeypatch):
        """The same exchange `app/server/oauth.py` does — which is what "the
        same principal as the app" means in practice."""
        seen = {}

        class FakeResponse:
            status_code = 200
            text = ""

            @staticmethod
            def json():
                return {"access_token": "exchanged", "expires_in": 3600}

        class FakeClient:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, url, data=None, auth=None):
                seen.update(url=url, data=data, auth=auth)
                return FakeResponse()

        import httpx

        monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

        credential = AppCredential(
            env={
                "DBX_OAUTH_CLIENT_ID": "sp-id",
                "DBX_OAUTH_CLIENT_SECRET": "sp-secret",
                "DATABRICKS_HOST": "dbc-x.cloud.databricks.com",
            }
        )
        assert await credential.token() == "exchanged"
        assert seen["url"] == "https://dbc-x.cloud.databricks.com/oidc/v1/token"
        assert seen["data"]["grant_type"] == "client_credentials"
        assert seen["auth"] == ("sp-id", "sp-secret")

    async def test_the_databricks_spellings_are_accepted_too(self, monkeypatch):
        """Databricks Apps injects DATABRICKS_CLIENT_ID/SECRET under those
        names; a job configured the same way should not need new ones."""
        credential = AppCredential(
            env={"DATABRICKS_CLIENT_ID": "id", "DATABRICKS_CLIENT_SECRET": "s"}
        )
        # No host, so the exchange cannot run and it falls through rather than
        # raising — the point being that the names were recognised at all.
        assert await credential.token() is None

    async def test_an_earlier_source_wins(self):
        credential = AppCredential(
            env={"DBX_APP_OAUTH_TOKEN": "explicit", "DATABRICKS_TOKEN": "pat"}
        )
        assert await credential.token() == "explicit"

    async def test_a_source_that_raises_falls_through_to_the_next(self, monkeypatch):
        """Every source is optional: `dbutils` off a workspace, an OIDC
        endpoint that refuses, a runtime with no identity at all."""

        async def boom():
            raise RuntimeError("HTTP 401")

        credential = AppCredential(env={"DATABRICKS_TOKEN": "pat"})
        monkeypatch.setattr(credential, "_from_client_credentials", boom)
        assert await credential.token() == "pat"

    async def test_nothing_anywhere_is_none_rather_than_an_error(self):
        """A job that cannot authenticate runs unobserved, exactly as it does
        when the app is simply down. The durable path never needed the app."""
        assert await AppCredential(env={}).token() is None


class TestCaching:
    async def test_the_token_is_not_re_fetched_on_every_call(self):
        """The push channel sends on every flush; an exchange per flush would
        be both slow and rude."""
        calls = []

        credential = AppCredential(env={}, now=Clock())

        async def once():
            calls.append(1)
            return "tok", 3600.0

        credential._from_env = once
        assert await credential.token() == "tok"
        assert await credential.token() == "tok"
        assert len(calls) == 1

    async def test_it_is_re_fetched_before_it_expires(self):
        clock = Clock()
        calls = []
        credential = AppCredential(env={}, now=clock)

        async def each_time():
            calls.append(1)
            return f"tok-{len(calls)}", 3600.0

        credential._from_env = each_time
        assert await credential.token() == "tok-1"

        # 3600s TTL less the 300s skew: still valid just before, stale after.
        clock.t = 3299.0
        assert await credential.token() == "tok-1"
        clock.t = 3301.0
        assert await credential.token() == "tok-2"

    @pytest.mark.parametrize("ttl", [1.0, 10.0, 120.0])
    async def test_a_short_lived_token_is_still_cached_for_a_while(self, ttl):
        """`ttl - skew` is negative for anything under five minutes, which
        would mean re-fetching on literally every call."""
        clock = Clock()
        credential = AppCredential(env={}, now=clock)

        async def fetch():
            return "tok", ttl

        credential._from_env = fetch
        await credential.token()
        assert credential._expires_at > 0
