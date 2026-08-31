"""Credentials for the app's ingress. Two of them, on two headers.

**The SDK does the OAuth.** This module used to be 193 lines that tried four
token sources in order, cached the result, and tracked its own expiry. All of
that is `databricks-sdk`'s job and it does it better — `Config.authenticate()`
returns fresh headers and handles refresh, and `Config()` with no arguments
walks the same default chain (env vars, a job's runtime identity, a PAT,
client credentials) that the hand-rolled version was reimplementing.

`docs/v4-rewrite-plan.md` said to adopt the SDK for exactly this and gave the
reason: hand-rolling it cost 343 lines and two deploy bugs. What is left here
is the part the SDK does not know about — that this platform needs *two*
credentials, on *two* headers, and why.

## Why two

`Authorization` belongs to the Databricks Apps **proxy**, which sits in front
of the app. It answers an unauthenticated upgrade with a **302 to the OAuth
login page** — never a 401 — so a job with no identity does not get a clean
refusal, it gets a redirect that names nothing. See `job/ws.py::diagnose`.

`X-DBX-App-Token` is the **app's own** shared secret, checked in
`app/server/routes/rpc.py` long after the proxy has already decided. Putting
the shared secret in `Authorization` is not a fallback — it is a rejected
handshake, because the proxy will not accept a non-OAuth value there.

A job with no Databricks identity runs **unobserved**, which is the same state
as the app being down: normal, not degraded.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

__all__ = ["APP_TOKEN_HEADER", "ingress_headers", "auth_headers"]

#: Where the app's own shared secret travels. NOT `Authorization` — see above.
APP_TOKEN_HEADER = "X-DBX-App-Token"


def auth_headers(host: str | None = None, config: Any = None) -> dict[str, str]:
    """`{"Authorization": "Bearer ..."}` from the SDK, or `{}` if there is no
    identity to be had.

    Never raises. A job that cannot authenticate should run unobserved rather
    than fail — the durable path does not care, and a run that dies because
    nobody was watching would be the tail wagging the dog.

    `config` is injectable so tests need no Databricks anything.
    """
    try:
        if config is None:
            from databricks.sdk.core import Config

            # No arguments beyond the host: the SDK's default chain already
            # covers a job's runtime identity, DATABRICKS_TOKEN, a PAT, and
            # client credentials. Naming sources here would be reimplementing
            # it, which is what the 193 lines this replaced were doing.
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


def ingress_headers(
    app_token: str | None, host: str | None = None, config: Any = None
) -> dict[str, str]:
    """Both halves, on their own headers.

    Synchronous, unlike the version this replaces. That was `async` because v3's
    harness was asyncio; the socket thread now owns no event loop, and making
    the caller bridge one to fetch a token was a cost with nothing on the other
    side of it.
    """
    headers = auth_headers(host, config)
    if app_token:
        headers[APP_TOKEN_HEADER] = app_token
    return headers
