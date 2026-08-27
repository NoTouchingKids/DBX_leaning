"""The Databricks identity a job presents to reach the app.

Two different authentications happen on one request, and conflating them is
what makes this subtle:

1. **The Databricks Apps ingress** stands in front of the app and lets nothing
   through without a Databricks OAuth token. It authenticates *who is
   calling* — a service principal — and it reads `Authorization: Bearer`.
2. **The app's own ingress check** (`app/server/routes/ingest.py`) authenticates
   *the job process*, with the shared `DBX_APP_TOKEN` the app hands each run
   as a job parameter at trigger time. That is not a Databricks identity and
   the proxy would reject it.

Both are needed, and there is only one `Authorization` header, so the shared
secret moved to `X-DBX-App-Token` and this module supplies the other half.

Where the identity comes from
-----------------------------

Any of these, in order, because a job can legitimately have any of them:

- an OAuth token handed in directly (`DBX_APP_OAUTH_TOKEN`) — an escape hatch,
  and what the local dev stack does not need at all;
- client credentials (`DBX_OAUTH_CLIENT_ID`/`SECRET`, or the `DATABRICKS_`
  spellings), exchanged at `/oidc/v1/token`. This is the "same principal as
  the app" case, and the exchange is the same one `app/server/oauth.py` does;
- a personal access token (`DATABRICKS_TOKEN`);
- the job's own runtime identity, via `dbutils`. This is the "use the job's
  auth" case and the one with no secret to distribute anywhere — it is the
  identity the task already runs as.

Whichever it is, **that principal needs CAN_USE on the app**, or the proxy
refuses the handshake and the run proceeds unobserved. Which is not a
failure: the durable path never depended on the app being reachable.

Nothing here is fatal. A job that cannot authenticate logs why and runs
without a live channel, exactly as it does when the app is simply down.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

log = logging.getLogger(__name__)

__all__ = ["AppCredential", "APP_TOKEN_HEADER"]

#: Where the app's own shared secret travels. NOT `Authorization`, which the
#: Databricks Apps proxy claims for the OAuth token — presenting the shared
#: secret there means the proxy rejects the handshake before the app ever sees
#: it, and the run silently goes unobserved.
APP_TOKEN_HEADER = "X-DBX-App-Token"

#: Refresh this long before expiry, so a token cannot go stale mid-request.
_REFRESH_SKEW_S = 300.0
_DEFAULT_TTL_S = 3600.0


class AppCredential:
    """The OAuth token for the Apps ingress, fetched once and reused.

    Cached rather than fetched per message: the HTTP-push channel sends on
    every flush, and a token exchange per flush would be both slow and rude.
    """

    def __init__(
        self,
        *,
        host: str | None = None,
        env: dict[str, str] | None = None,
        now: Any = time.monotonic,
    ) -> None:
        self._env = dict(os.environ if env is None else env)
        self._host = (host or self._env.get("DATABRICKS_HOST") or "").strip().rstrip("/")
        if self._host and not self._host.startswith("http"):
            self._host = f"https://{self._host}"
        self._now = now
        self._token: str | None = None
        self._expires_at = 0.0
        #: Which source answered, for the log line and for `/healthz`-shaped
        #: reporting. None until the first successful fetch.
        self.source: str | None = None

    def _get(self, *names: str) -> str | None:
        for name in names:
            value = (self._env.get(name) or "").strip()
            if value:
                return value
        return None

    async def token(self) -> str | None:
        """A token, or None if this job has no Databricks identity to offer."""
        if self._token is not None and self._now() < self._expires_at:
            return self._token

        for source, fetch in (
            ("DBX_APP_OAUTH_TOKEN", self._from_env),
            ("client credentials", self._from_client_credentials),
            ("DATABRICKS_TOKEN", self._from_pat),
            ("the job's runtime identity", self._from_dbutils),
        ):
            try:
                found = await fetch()
            except Exception as exc:  # noqa: BLE001 - every source is optional
                log.info("no app credential from %s: %s", source, exc)
                continue
            if not found:
                continue
            token, ttl = found
            self._token = token
            self._expires_at = self._now() + max(ttl - _REFRESH_SKEW_S, ttl / 2)
            if self.source != source:
                log.info("app credential from %s", source)
            self.source = source
            return token

        log.info(
            "no Databricks credential for the app ingress; if the app is behind "
            "the Apps proxy this run will go unobserved (durable path unaffected)"
        )
        return None

    async def _from_env(self) -> tuple[str, float] | None:
        token = self._get("DBX_APP_OAUTH_TOKEN")
        return (token, _DEFAULT_TTL_S) if token else None

    async def _from_client_credentials(self) -> tuple[str, float] | None:
        """The same exchange `app/server/oauth.py` does, against the same
        endpoint — which is what "the same principal as the app" means."""
        client_id = self._get("DBX_OAUTH_CLIENT_ID", "DATABRICKS_CLIENT_ID")
        secret = self._get("DBX_OAUTH_CLIENT_SECRET", "DATABRICKS_CLIENT_SECRET")
        if not (client_id and secret and self._host):
            return None

        import httpx

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{self._host}/oidc/v1/token",
                data={
                    "grant_type": "client_credentials",
                    "scope": "all-apis",
                },
                auth=(client_id, secret),
            )
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code} {resp.text[:200]}")
        payload = resp.json()
        token = payload.get("access_token")
        if not token:
            raise RuntimeError("no access_token in the response")
        return token, float(payload.get("expires_in") or _DEFAULT_TTL_S)

    async def _from_pat(self) -> tuple[str, float] | None:
        token = self._get("DATABRICKS_TOKEN")
        return (token, _DEFAULT_TTL_S) if token else None

    async def _from_dbutils(self) -> tuple[str, float] | None:
        """The identity the task already runs as, with no secret anywhere.

        Only available inside a Databricks runtime, which is the only place it
        is wanted — everywhere else the import fails and the caller moves on.
        """
        from pyspark.dbutils import DBUtils  # noqa: PLC0415
        from pyspark.sql import SparkSession  # noqa: PLC0415

        spark = SparkSession.getActiveSession() or SparkSession.builder.getOrCreate()
        context = DBUtils(spark).notebook.entry_point.getDbutils().notebook().getContext()
        token = context.apiToken().get()
        # The runtime does not say how long this is good for. An hour is the
        # usual lifetime and the skew above keeps a stale one from being used.
        return (token, _DEFAULT_TTL_S) if token else None


async def ingress_headers(
    app_token: str | None, credential: AppCredential | None
) -> dict[str, str]:
    """Both halves, on their own headers.

    The shared secret must not go in `Authorization`: that is the proxy's, and
    a non-OAuth value there is a rejected handshake rather than a fallback.
    """
    headers: dict[str, str] = {}
    if credential is not None:
        token = await credential.token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
    if app_token:
        headers[APP_TOKEN_HEADER] = app_token
    return headers
