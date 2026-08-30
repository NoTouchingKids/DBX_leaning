"""What the trigger endpoint hands a job so it can authenticate.

The job's two live paths — the WebSocket through the Databricks Apps proxy,
and Lakebase, which takes an OAuth token as its Postgres password — both need
a Databricks OAuth token. The job's own runtime identity cannot supply one:
`dbutils` returns `context.apiToken()`, a workspace REST API token, and a real
workspace answered it with a redirect to a login page and a password rejection
respectively.

So the app forwards its own client credentials and the job mints tokens with
them. These tests pin the forwarding and the two ways it can be wrong.
"""

from __future__ import annotations

import pytest

from server.routes.runs import JOB_PARAMETER_NAMES, build_job_parameters


class FakeConfig:
    catalog = "main"
    schema = "dbx_leaning"
    public_url = "https://app.example"
    job_token = "shared"

    def __init__(self, client_id=None, secret=None):
        self.oauth_client_id = client_id
        self.oauth_client_secret = secret

    @property
    def has_client_credentials(self) -> bool:
        return bool(self.oauth_client_id and self.oauth_client_secret)


def parameters(**kwargs) -> dict[str, str]:
    return build_job_parameters("r1", "heartbeat", {}, FakeConfig(**kwargs))


def test_the_apps_oauth_identity_travels_to_the_job():
    sent = parameters(client_id="an-application-id", secret="a-secret")

    assert sent["DBX_OAUTH_CLIENT_ID"] == "an-application-id"
    assert sent["DBX_OAUTH_CLIENT_SECRET"] == "a-secret"


def test_an_app_with_no_credentials_sends_neither():
    """Not an empty string — absent.

    `job/auth.py` treats an empty value as "not configured here" and moves on,
    but sending the key at all would say the app has an identity it does not,
    and the job's log line would then name the wrong reason it has no token.
    """
    sent = parameters()

    assert "DBX_OAUTH_CLIENT_ID" not in sent
    assert "DBX_OAUTH_CLIENT_SECRET" not in sent


def test_half_a_credential_is_not_forwarded():
    """An id without a secret cannot mint anything, and forwarding it would
    make the job try the exchange and fail on a 401 rather than skip a source
    it was never configured for."""
    assert "DBX_OAUTH_CLIENT_ID" not in parameters(client_id="an-application-id")
    assert "DBX_OAUTH_CLIENT_SECRET" not in parameters(secret="a-secret")


@pytest.mark.parametrize("name", ["DBX_OAUTH_CLIENT_ID", "DBX_OAUTH_CLIENT_SECRET"])
def test_both_names_are_declared_parameters(name):
    """Databricks rejects a run-now parameter a job has not declared, so a name
    the app sends and the job files do not declare breaks EVERY trigger.
    `tests/deploy/test_bundle.py` holds the other end of this."""
    assert name in JOB_PARAMETER_NAMES


def test_everything_sent_is_a_declared_parameter():
    sent = set(parameters(client_id="an-application-id", secret="a-secret"))
    assert sent <= set(JOB_PARAMETER_NAMES), sent - set(JOB_PARAMETER_NAMES)
