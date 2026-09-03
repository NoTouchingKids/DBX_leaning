"""The shared ingress identity: one principal, one grant, any number of jobs.

A job's own runtime identity works and does not scale — every principal any
job runs as would need `CAN_USE` on the app. So a job may instead authenticate
as one shared service principal, BOTH halves of which live in a secret scope
and are read at run time with `dbutils.secrets.get`.

The rule these tests exist to hold is that **the secret is never a job
parameter**. Parameters come back from `databricks jobs get-run` and are shown
in the run UI, and a serverless task has no environment-variable field to use
instead — `databricks bundle schema` gives a `spark_python_task` exactly
`parameters`, `python_file` and `source`.
"""

from __future__ import annotations

import pathlib

import yaml

from job.auth import auth_headers, read_secret
from job.config import JobConfig

ROOT = pathlib.Path(__file__).resolve().parents[2]


class FakeConfig:
    """Stands in for `databricks.sdk.core.Config`, recording how it was built."""

    seen: dict = {}

    def __init__(self, **kwargs):
        FakeConfig.seen = kwargs
        self.auth_type = "oauth-m2m" if kwargs.get("client_id") else "runtime"

    def authenticate(self):
        return {"Authorization": "Bearer token-for-" + str(FakeConfig.seen.get("client_id"))}


# --- choosing an identity ---------------------------------------------------


class FakeM2M:
    """Stands in for `M2MTokenProvider` — no network, no clock."""

    def __init__(self, token="tok-from-m2m"):
        self._token = token
        self.calls = 0

    def token(self):
        self.calls += 1
        return self._token


def test_client_credentials_are_used_when_both_are_present():
    """The real branch: `client_id`/`client_secret` as KEYWORDS, the shape
    `job/ws.py::app_client` actually calls this with. A `config=` object is
    irrelevant here — the M2M path never looks at it, which is what
    distinguishes it from the SDK path below."""
    m2m = FakeM2M("tok-123")
    headers = auth_headers(
        "https://example.cloud.databricks.com",
        client_id="sp-123",
        client_secret="shhh",
        m2m=m2m,
    )
    assert headers == {"Authorization": "Bearer tok-123"}
    assert m2m.calls == 1


def test_a_provided_m2m_provider_is_reused_not_rebuilt():
    """The whole point of `job/ws.py` constructing one provider per run: this
    is what lets a reconnect a minute later reuse the cached token instead of
    exchanging a new one."""
    m2m = FakeM2M("tok-1")
    for _ in range(3):
        auth_headers("https://x", client_id="id", client_secret="secret", m2m=m2m)
    assert m2m.calls == 3, "auth_headers must ask the provider every time"
    # calls == 3 because FakeM2M does not cache; M2MTokenProvider's OWN cache
    # is covered in tests/job/test_m2m.py. What this test pins is narrower:
    # auth_headers must not construct a SECOND provider behind the caller's
    # back when one is supplied.


def test_no_workspace_host_means_no_exchange_is_attempted():
    """The token endpoint is built from the workspace host; without one there
    is nowhere to send the request, and this must not try anyway."""
    m2m = FakeM2M()
    assert auth_headers(None, client_id="id", client_secret="secret", m2m=m2m) == {}
    assert m2m.calls == 0


def test_half_a_credential_is_not_a_credential(monkeypatch):
    """A client id with no secret must fall back, not fail.

    Handing the id alone to the SDK makes it raise, which would take down a run
    that could perfectly well have presented the identity it already has. This
    is not hypothetical: `read_secret` returns None when the secret cannot be
    read, so "id present, secret missing" is the expected shape of a
    misconfigured scope.
    """
    built: list[dict] = []

    class Recording:
        def __init__(self, **kwargs):
            built.append(kwargs)
            self.auth_type = "runtime"

        def authenticate(self):
            return {"Authorization": "Bearer runtime"}

    monkeypatch.setattr("databricks.sdk.core.Config", Recording)

    headers = auth_headers("https://x", client_id="sp-123", client_secret=None)

    assert headers == {"Authorization": "Bearer runtime"}
    assert "client_id" not in built[0], (
        "the id was passed without its secret; the SDK would raise instead of "
        "using the identity this job already has"
    )


def test_no_credentials_at_all_is_the_runtime_identity():
    headers = auth_headers("https://x", config=FakeConfig())
    assert headers == {"Authorization": "Bearer token-for-None"}


def test_a_secret_that_cannot_be_read_is_not_an_error(monkeypatch):
    """A job then presents its runtime identity, and if that is not enough the
    run goes unobserved — a normal state, not a failure.

    Both routes are stubbed to raise rather than left to fail for real: the
    fallback path builds a `WorkspaceClient`, and an offline test must not make
    an API call to prove it handles one failing.
    """

    class Boom:
        def __init__(self, *a, **k):
            raise RuntimeError("no workspace here")

    monkeypatch.setattr("databricks.sdk.WorkspaceClient", Boom)
    monkeypatch.delitem(__import__("sys").modules, "databricks.sdk.runtime", raising=False)

    assert read_secret("no-such-scope", "no-such-key") is None


# --- the config contract ----------------------------------------------------


def test_the_job_reads_locations_not_credentials():
    """Neither half of the identity is in the config — only where to find it."""
    cfg = JobConfig.from_env(
        {
            "DBX_RUN_ID": "r1",
            "DBX_MODEL": "heartbeat",
            "DBX_OAUTH_SECRET_SCOPE": "dbx-leaning",
            "DBX_OAUTH_CLIENT_ID_KEY": "oauth-client-id",
            "DBX_OAUTH_SECRET_KEY": "oauth-client-secret",
        }
    )
    assert cfg.oauth_secret_scope == "dbx-leaning"
    assert cfg.oauth_client_id_key == "oauth-client-id"
    assert cfg.oauth_secret_key == "oauth-client-secret"
    assert cfg.has_ingress_identity

    for attr in ("oauth_client_id", "oauth_client_secret"):
        assert not hasattr(cfg, attr), (
            f"{attr} holds a credential value; the config carries locations only"
        )


def test_a_partial_configuration_is_not_a_partial_identity():
    """Two names out of three cannot authenticate anything, so it falls back to
    the runtime identity rather than half-trying and failing."""
    cfg = JobConfig.from_env(
        {
            "DBX_RUN_ID": "r1",
            "DBX_MODEL": "heartbeat",
            "DBX_OAUTH_SECRET_SCOPE": "dbx-leaning",
        }
    )
    assert not cfg.has_ingress_identity


def test_all_three_absent_is_a_supported_deploy():
    cfg = JobConfig.from_env({"DBX_RUN_ID": "r1", "DBX_MODEL": "heartbeat"})
    assert not cfg.has_ingress_identity
    assert cfg.oauth_secret_scope is None


# --- the job definition -----------------------------------------------------


def _heartbeat_job() -> dict:
    doc = yaml.safe_load((ROOT / "resources" / "model_heartbeat.job.yml").read_text())
    return doc["resources"]["jobs"]["model_heartbeat"]


def test_no_job_parameter_carries_a_secret_value():
    """The rule this whole file exists for.

    A job parameter is readable by anyone who can see a run. Names are fine —
    a client id identifies, a scope and key locate — but a value that IS the
    credential must never appear here, and neither must a `{{secrets/...}}`
    reference, which would resolve into exactly that place.
    """
    params = {p["name"]: str(p.get("default", "")) for p in _heartbeat_job()["parameters"]}

    assert "DBX_OAUTH_SECRET_SCOPE" in params
    assert "DBX_OAUTH_CLIENT_ID_KEY" in params
    assert "DBX_OAUTH_SECRET_KEY" in params
    for forbidden in ("DBX_OAUTH_CLIENT_SECRET", "DBX_OAUTH_CLIENT_ID"):
        assert forbidden not in params, (
            f"{forbidden} is a credential VALUE; job parameters carry locations only"
        )

    for name, default in params.items():
        assert "{{secrets/" not in default, (
            f"{name} resolves a secret into a job parameter, which is visible "
            f"in `jobs get-run` and the run UI"
        )


def test_every_parameter_reaches_the_task():
    """A parameter the task is not handed is a parameter that does nothing.

    Serverless tasks have no env vars, so each one has to be forwarded
    explicitly as a `KEY={{job.parameters.KEY}}` positional. Adding a
    parameter and forgetting this line fails silently: the job accepts it, the
    harness never sees it.
    """
    job = _heartbeat_job()
    declared = {p["name"] for p in job["parameters"]}
    forwarded = {arg.split("=", 1)[0] for arg in job["tasks"][0]["spark_python_task"]["parameters"]}
    assert declared == forwarded, f"declared but not forwarded: {declared - forwarded}"
