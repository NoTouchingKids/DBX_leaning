"""The two credentials, and what happens when there is only one.

Short, because the OAuth is the SDK's now. What is worth testing is the part
the SDK does not know about: that this platform needs two credentials on two
different headers, and that a job with neither still runs.
"""

from __future__ import annotations

from job.auth import APP_TOKEN_HEADER, auth_headers, ingress_headers


class FakeConfig:
    def __init__(self, headers=None, raises=None):
        self._headers = headers
        self._raises = raises

    def authenticate(self):
        if self._raises:
            raise self._raises
        return self._headers


def test_both_credentials_travel_on_their_own_headers():
    """`Authorization` is the Apps proxy's; the shared secret is the app's own.

    Putting the secret in `Authorization` is not a fallback — the proxy will
    not accept a non-OAuth value there, so the handshake is rejected before
    the app ever sees it.
    """
    headers = ingress_headers("s3cret", config=FakeConfig({"Authorization": "Bearer abc"}))
    assert headers["Authorization"] == "Bearer abc"
    assert headers[APP_TOKEN_HEADER] == "s3cret"


def test_no_databricks_identity_is_a_normal_state_not_a_failure():
    """A job that cannot authenticate runs unobserved, which is the same state
    as the app being down. Raising here would let "nobody was watching" kill a
    run whose durable path is entirely fine."""
    headers = ingress_headers("s3cret", config=FakeConfig(raises=RuntimeError("no creds")))

    assert "Authorization" not in headers
    assert headers[APP_TOKEN_HEADER] == "s3cret", "the app's own secret still travels"


def test_no_app_token_still_sends_the_identity():
    """An empty DBX_APP_TOKEN is the development posture — the app accepts
    everyone — but the proxy in front of it does not, so the identity is still
    required."""
    headers = ingress_headers(None, config=FakeConfig({"Authorization": "Bearer abc"}))
    assert headers == {"Authorization": "Bearer abc"}


def test_a_job_with_nothing_at_all_gets_an_empty_dict_rather_than_an_error():
    assert ingress_headers(None, config=FakeConfig(raises=RuntimeError("nope"))) == {}


def test_auth_headers_never_raises_whatever_the_sdk_does():
    for boom in (RuntimeError("x"), ValueError("y"), OSError("z")):
        assert auth_headers(config=FakeConfig(raises=boom)) == {}
