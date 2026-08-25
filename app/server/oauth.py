"""An OAuth token for the app's own service principal, fetched when needed.

Lakebase accepts a **short-lived Databricks OAuth token** as the Postgres
password, and an instance created with ``enable_pg_native_login: false`` — the
default — accepts nothing else. There is no password to put in a secret.

That is not a small detail, because a token is not a setting. The obvious
shape, and the one this app had, is to read the credential from the
environment once at startup and build a connection string from it. A
Databricks App runs for up to 24 hours; a token lasts around one. So the
obvious shape works for the first hour, then fails every single Postgres
operation, long after anyone would connect the failure to startup.

So the token is resolved **per connection** instead, which is also why
``store.py`` opens a connection per operation rather than pooling. This class
is what does the resolving: it caches a token until shortly before it expires
and fetches a new one after that, so the common case is a dictionary lookup
and the uncommon case is one HTTP round trip.

The exchange is `client_credentials` against the workspace's OIDC endpoint —
a plain form POST over ``httpx``, in keeping with this app carrying no
``databricks-sdk`` (see CLAUDE.md). Databricks Apps injects the service
principal's id and secret into the app's environment; ``config.py`` reads them
under the names below, and both are overridable because that injection is a
platform behaviour this repo has not verified against a real workspace.

When there is no client id and secret, nothing here is used: `config.py` falls
back to a static password from ``DBX_LAKEBASE_PASSWORD`` or
``DATABRICKS_TOKEN``, which is what the local dev stack and a hand-run
instance with native login enabled need.
"""

from __future__ import annotations

import logging
import time

import httpx

log = logging.getLogger(__name__)

__all__ = ["OAuthTokenProvider", "TokenUnavailable"]

#: Refresh this long before the token actually expires. A request that starts
#: with four minutes left and reaches Postgres with none is the kind of
#: failure that shows up as an occasional, unreproducible auth error.
REFRESH_SKEW_S = 300.0

#: What a token is assumed to last when the response does not say. Short on
#: purpose: guessing low costs an extra round trip, guessing high costs
#: failed connections.
DEFAULT_TTL_S = 600.0


class TokenUnavailable(RuntimeError):
    """The token exchange failed. Raised rather than returning an empty
    string, which Postgres would report as a password mismatch — sending
    whoever reads the log looking for the wrong problem."""


class OAuthTokenProvider:
    def __init__(
        self,
        host: str,
        client_id: str,
        client_secret: str,
        *,
        scope: str = "all-apis",
        http: httpx.AsyncClient | None = None,
        now=time.monotonic,
    ) -> None:
        self._host = host.rstrip("/")
        self._client_id = client_id
        self._client_secret = client_secret
        self._scope = scope
        self._http = http
        self._now = now
        self._token: str | None = None
        self._expires_at: float = 0.0

    @property
    def url(self) -> str:
        return f"{self._host}/oidc/v1/token"

    async def token(self) -> str:
        """A valid token, cached until it is nearly expired."""
        if self._token is not None and self._now() < self._expires_at:
            return self._token

        access, ttl = await self._fetch()
        self._token = access
        # max(): a TTL shorter than the skew would otherwise cache a token for
        # a negative duration, refetching on every single connection.
        self._expires_at = self._now() + max(ttl - REFRESH_SKEW_S, ttl / 2)
        return access

    async def _fetch(self) -> tuple[str, float]:
        client = self._http or httpx.AsyncClient(timeout=30.0)
        try:
            response = await client.post(
                self.url,
                data={"grant_type": "client_credentials", "scope": self._scope},
                auth=(self._client_id, self._client_secret),
            )
        except httpx.HTTPError as exc:
            raise TokenUnavailable(f"could not reach {self.url}: {exc}") from exc
        finally:
            if self._http is None:
                await client.aclose()

        if response.status_code != httpx.codes.OK:
            # The body carries OAuth's own error code — `invalid_client` for a
            # wrong secret, `invalid_scope` for a scope the principal does not
            # have. Worth more than the status alone, and it contains no
            # credential.
            raise TokenUnavailable(
                f"{self.url} answered {response.status_code}: {response.text[:200]}"
            )

        payload = response.json()
        access = payload.get("access_token")
        if not access:
            raise TokenUnavailable(f"{self.url} returned no access_token: {sorted(payload)}")

        try:
            ttl = float(payload.get("expires_in", DEFAULT_TTL_S))
        except (TypeError, ValueError):
            ttl = DEFAULT_TTL_S
        log.debug("obtained an OAuth token, expires_in=%s", ttl)
        return access, ttl
