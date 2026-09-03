"""The credential for the app's ingress. One of them, and the SDK gets it.

**The SDK does the OAuth.** This module used to be 193 lines that tried four
token sources in order, cached the result, and tracked its own expiry. All of
that is `databricks-sdk`'s job and it does it better — `Config.authenticate()`
returns fresh headers and handles refresh, and `Config()` with no arguments
walks the same default chain (env vars, a job's runtime identity, a PAT,
client credentials) that the hand-rolled version was reimplementing.

`docs/v4-rewrite-plan.md` said to adopt the SDK for exactly this and gave the
reason: hand-rolling it cost 343 lines and two deploy bugs. What is left here
is the part the SDK does not decide: WHICH identity a job presents, and where
its credential comes from.

## One identity for every job

By default the SDK resolves the job's own runtime identity — the deployed log
line reads `auth_type=runtime`. That works, and it does not scale: every
principal any job runs as needs `CAN_USE` on the app, which is per-job admin
toil that grows with the number of models.

So a job may instead present a **shared ingress service principal**, named by
`DBX_OAUTH_CLIENT_ID` with its secret read at run time. One principal, one
`CAN_USE` grant, any number of jobs — including jobs in other bundles or other
repositories, which is the case the tag-based discovery was already built for.

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

So the parameters carry only NAMES — a client id, a scope, a key, none of them
secret — and the value is read at run time with `dbutils.secrets.get`. The
secret appears in no job definition, no run history and no log.

## Why one

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
from typing import Any

log = logging.getLogger(__name__)

__all__ = ["auth_headers", "read_secret"]


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
) -> dict[str, str]:
    """`{"Authorization": "Bearer ..."}` from the SDK, or `{}` if there is no
    identity to be had.

    With `client_id` and `client_secret` the SDK does OAuth M2M as that
    service principal — the shared ingress identity described above. Without
    them it walks its default chain, which in a job resolves the job's own
    runtime identity.

    Never raises. A job that cannot authenticate should run unobserved rather
    than fail — the durable path does not care, and a run that died because
    nobody was watching would be the tail wagging the dog.

    `config` is injectable so tests need no Databricks anything.
    """
    try:
        if config is None:
            from databricks.sdk.core import Config

            if client_id and client_secret:
                # Explicit, and only when BOTH are present. A client id alone
                # is not a credential, and passing it without the secret would
                # make the SDK fail rather than fall through to the identity
                # this job already has.
                config = Config(host=host, client_id=client_id, client_secret=client_secret)
            else:
                # The SDK's default chain already covers a job's runtime
                # identity, DATABRICKS_TOKEN, a PAT and client credentials from
                # the environment. Naming sources here would be reimplementing
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
