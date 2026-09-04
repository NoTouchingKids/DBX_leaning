"""The credential for the app's ingress: the job's own identity, or a shared one.

## Two paths, two mechanisms, on purpose

**No client credentials → the SDK's default chain.** `Config(host=host).authenticate()`
resolves the job's own runtime identity — the deployed log line reads
`auth_type=runtime`, verified against a real run. That is a broad "figure out
who I am from wherever I am running" problem, which is exactly what the SDK is
for; hand-rolling it cost 343 lines and two deploy bugs before it was dropped
in favour of `Config()`. See `docs/v4-rewrite-plan.md`, "Auth: the SDK, for
credentials only".

**Client credentials present → one plain `httpx` POST, not the SDK.**
`M2MTokenProvider` below exchanges a service principal's id and secret for a
token at `/oidc/v1/token` itself. That is a single, fully-specified REST call —
`grant_type=client_credentials`, HTTP Basic auth, a JSON body with
`access_token` and `expires_in` — not a multi-source resolution problem, so
routing it through the SDK's `Config(client_id=..., client_secret=...)` buys
nothing but another way for the same exchange to fail. `app/server/oauth.py`
already does the identical exchange for the app's own Lakebase credential, one
line for one line except sync instead of async; `M2MTokenProvider` mirrors it
rather than inventing a second shape for the same problem.

## One identity for every job

By default a job's runtime identity works and does not scale: every principal
any job runs as needs `CAN_USE` on the app, which is per-job admin toil that
grows with the number of models.

So a job may instead present a **shared ingress service principal**. One
principal, one `CAN_USE` grant, any number of jobs — including jobs in other
bundles or other repositories, which is the case the tag-based discovery was
already built for.

`run_as` on the job is the other way to get one identity, and needs no secret
at all. It is not equivalent: it changes the identity the job uses for
EVERYTHING, data access included. Client credentials keep the ingress identity
separate from the run identity, which is why they are the option wired up here.

## Why the secret is not a job parameter

Because a serverless task has nowhere else to put it, and that nowhere is not
safe. `databricks bundle schema` gives a `spark_python_task` exactly three
fields — `parameters`, `python_file`, `source` — with no environment-variable
map anywhere on the task or its environment (`spark_env_vars` belongs to
`new_cluster`, which serverless has none of).

And job parameters are **visible**: they come back in `jobs get-run`, and they
are shown in the run UI. A client secret passed that way is readable by anyone
who can see a run.

So the parameters carry only NAMES — a secret scope and two key names, none of
them secret — and both halves of the identity are read at run time with
`dbutils.secrets.get`. Neither appears in a job definition, a run parameter or
a log.

## Why one credential, not two

There used to be two: an OAuth identity for the proxy, and `X-DBX-App-Token`,
a shared secret the app checked itself. The second is gone, and its absence is
the point.

`Authorization` belongs to the Databricks Apps **proxy**, which sits in front
of the app and lets nothing through without a Databricks OAuth token from a
principal holding `CAN_USE`. That is platform-enforced, and the 302 it answers
an unauthenticated upgrade with — never a 401 — is the proof; see
`job/ws.py::diagnose`.

So by the time a request reaches the app, the platform has already
authenticated it. A second shared secret added a secret to create and rotate, a
job parameter to carry it, a deploy-time resource that 404s the deploy when
absent, and — worst — a silent failure: an unset token made the app accept
EVERYONE, so a typo opened the ingress rather than closing it.

A job with no Databricks identity runs **unobserved**, which is the same state
as the app being down: normal, not degraded.
"""

from __future__ import annotations

import base64
import logging
import time
from typing import Any

log = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_TTL_S",
    "REFRESH_SKEW_S",
    "M2MTokenProvider",
    "TokenUnavailable",
    "auth_headers",
    "read_secret",
]

#: Refresh this long before the token actually expires. A request that starts
#: with four minutes left and reaches the app after it has expired is the kind
#: of failure that shows up as an occasional, unreproducible 302.
#:
#: Duplicated from `app/server/oauth.py` rather than imported from it —
#: deliberately. The job installs this repo as ONE distribution, so `server`
#: is technically importable from here, but the job and the app are two
#: services and the job should not come to depend on the app's module simply
#: because setuptools happens to make it reachable. Two constants in
#: agreement is a smaller cost than that coupling.
REFRESH_SKEW_S = 300.0

#: What a token is assumed to last when the response does not say. Short on
#: purpose: guessing low costs an extra round trip, guessing high costs failed
#: connections.
DEFAULT_TTL_S = 600.0


class TokenUnavailable(RuntimeError):
    """The M2M exchange failed. Raised rather than returning an empty string,
    so `auth_headers` can log a real reason instead of a silent 302."""


class M2MTokenProvider:
    """OAuth `client_credentials` against the workspace's own OIDC endpoint —
    a plain form POST over `httpx`, exactly the shape in Databricks' own M2M
    example: `POST /oidc/v1/token`, `Authorization: Basic base64(id:secret)`,
    body `grant_type=client_credentials&scope=all-apis`.

    Synchronous, unlike `app/server/oauth.py::OAuthTokenProvider` which this
    otherwise mirrors: the job's socket thread owns no event loop (see
    `job/ws.py`), so there is nothing here to await.

    Caches a token until shortly before it expires. `job/ws.py::app_client`
    constructs ONE of these per run and closes over it, so a reconnect an hour
    in gets a fresh token and a reconnect a minute in gets the same one — the
    property the SDK's own caching used to provide, before the M2M path
    stopped going through the SDK.
    """

    def __init__(
        self,
        host: str,
        client_id: str,
        client_secret: str,
        *,
        scope: str = "all-apis",
        http: Any = None,
        now: Any = time.monotonic,
    ) -> None:
        self._host = (
            host.removeprefix("https://")
            .removeprefix("http://")
            .rstrip("/")
            .split("?")[0]
            .rstrip("/")
        )
        self._client_id = client_id
        self._client_secret = client_secret
        self._scope = scope
        self._http = http
        self._now = now
        self._token: str | None = None
        self._expires_at: float = 0.0

    @property
    def url(self) -> str:
        return f"https://{self._host}/oidc/v1/token"

    def token(self) -> str:
        """A valid token, cached until it is nearly expired."""
        if self._token is not None and self._now() < self._expires_at:
            return self._token

        access, ttl = self._fetch()
        self._token = access
        # max(): a TTL shorter than the skew would otherwise cache a token for
        # a negative duration, refetching on every single connection attempt.
        self._expires_at = self._now() + max(ttl - REFRESH_SKEW_S, ttl / 2)
        return access

    def _fetch(self) -> tuple[str, float]:
        import httpx

        client = self._http or httpx.Client(timeout=10.0)
        try:
            response = client.post(
                self.url,
                data={"grant_type": "client_credentials", "scope": self._scope},
                auth=(self._client_id, self._client_secret),
            )
        except httpx.HTTPError as exc:
            raise TokenUnavailable(f"could not reach {self.url}: {exc}") from exc
        finally:
            if self._http is None:
                client.close()

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
        log.debug("obtained an M2M token from %s, expires_in=%s", self.url, ttl)
        return access, ttl


def read_secret(scope: str, key: str) -> str | None:
    """A secret's value, read the one way this project reads secrets.

    **`dbutils.secrets.get` is the mechanism.** Not a job parameter, not an
    environment variable, not a file on the volume. That is a project rule
    rather than a local preference, and it has two teeth:

      * a job parameter is VISIBLE — it comes back from `databricks jobs
        get-run` and is shown in the run UI, so a secret placed there is
        readable by anyone who can see a run;
      * a serverless task has nowhere else to put one anyway. `databricks
        bundle schema` gives a `spark_python_task` exactly `parameters`,
        `python_file` and `source`; `spark_env_vars` belongs to `new_cluster`,
        which serverless does not have.

    dbutils also redacts the value if it is printed, which an environment
    variable does not.

    The Secrets API below is a FALLBACK for one case and not a second
    mechanism: `job/local.py::run_local` runs off-platform — a laptop, a test —
    where there is no Databricks runtime and therefore no `dbutils`. It returns
    base64 rather than plaintext, which is the one difference worth
    remembering. Delete it and the job path is unaffected.

    THE APP CANNOT FOLLOW THIS RULE, and that is not an oversight. A Databricks
    Apps container is not a Databricks runtime, so it has no `dbutils` at all;
    its only route is a declared secret resource surfaced as an env var
    (`value_from` in resources/app.yml). See `app/server/config.py`.

    Returns None rather than raising. A credential that cannot be read means
    the job presents its runtime identity instead, and a job that cannot
    authenticate at all runs unobserved — neither is a reason to fail a run
    whose durable path is fine.
    """
    try:
        from databricks.sdk.runtime import dbutils

        return dbutils.secrets.get(scope=scope, key=key)
    except Exception as exc:  # noqa: BLE001 - not in a runtime, or no access
        log.debug("dbutils could not read %s/%s (%s); trying the API", scope, key, exc)

    try:
        from databricks.sdk import WorkspaceClient

        raw = WorkspaceClient().secrets.get_secret(scope=scope, key=key).value
        return base64.b64decode(raw).decode() if raw else None
    except Exception as exc:  # noqa: BLE001 - a missing credential is not fatal
        log.info(
            "could not read the ingress credential from secret %s/%s (%s); "
            "falling back to this job's own runtime identity",
            scope,
            key,
            exc,
        )
        return None


def auth_headers(
    host: str | None = None,
    config: Any = None,
    *,
    client_id: str | None = None,
    client_secret: str | None = None,
    m2m: M2MTokenProvider | None = None,
) -> dict[str, str]:
    """`{"Authorization": "Bearer ..."}`, or `{}` if there is no identity to
    present.

    With `client_id` and `client_secret` this exchanges them for a token
    itself, via `M2MTokenProvider` — the shared ingress identity. `m2m` is that
    provider, injected so the caller controls its lifetime (one per run, so its
    cache is worth having) and so tests need no network. Built on the fly if
    omitted, which works but caches nothing across calls.

    Without credentials it falls back to the SDK's default chain, which in a
    job resolves the job's own runtime identity.

    Never raises. A job that cannot authenticate should run unobserved rather
    than fail — the durable path does not care, and a run that died because
    nobody was watching would be the tail wagging the dog.

    `config` is injectable so the SDK path needs no Databricks anything either.
    """
    if client_id and client_secret:
        if not host:
            log.info(
                "client credentials are configured but no workspace host was given; "
                "this run will go unobserved"
            )
            return {}
        try:
            token = (m2m or M2MTokenProvider(host, client_id, client_secret)).token()
        except Exception as exc:  # noqa: BLE001 - no identity is a normal state
            log.info(
                "M2M token exchange failed (%s); this run will go unobserved if the "
                "app is behind the Apps proxy. The durable path is unaffected.",
                exc,
            )
            return {}
        log.info("presenting a Databricks identity to the app ingress (auth_type=oauth-m2m)")
        return {"Authorization": f"Bearer {token}"}

    try:
        if config is None:
            from databricks.sdk.core import Config

            # The SDK's default chain already covers a job's runtime identity,
            # DATABRICKS_TOKEN, a PAT and client credentials from the
            # environment. Naming sources here would be reimplementing it,
            # which is what the 193 lines this module used to be were doing.
            config = Config(host=host) if host else Config()

        headers = config.authenticate()
    except Exception as exc:  # noqa: BLE001 - no identity is a normal state
        log.info(
            "no Databricks credential for the app ingress (%s); this run will go "
            "unobserved if the app is behind the Apps proxy. The durable path is "
            "unaffected.",
            exc,
        )
        return {}

    if not headers:
        log.info("the Databricks SDK returned no auth headers; running unobserved")
        return {}

    # Say WHICH identity, and say it once per attempt. Without this the only
    # signal that authentication worked is the ABSENCE of the failure line
    # above — so "no identity" and "an identity the app rejects" look identical
    # in a job log, and both present as a 302. That ambiguity cost a debugging
    # round on 2026-08-31.
    auth_type = getattr(config, "auth_type", None) or "unknown"
    log.info("presenting a Databricks identity to the app ingress (auth_type=%s)", auth_type)
    return dict(headers)
