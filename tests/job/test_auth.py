"""One credential, and what happens when there is none.

Short, because the OAuth is the SDK's now. What is left worth testing is the
part the SDK does not decide: that a job which cannot authenticate still runs.

This file used to assert the opposite shape — two credentials on two headers,
`Authorization` for the proxy and `X-DBX-App-Token` for the app's own check.
The second is gone. The Apps proxy refuses anything without a Databricks OAuth
token from a principal holding CAN_USE, so the app's own secret was a second
check on top of a platform-enforced one, and it failed open when unset.
"""

from __future__ import annotations

from job.auth import auth_headers


class FakeConfig:
    def __init__(self, headers=None, raises=None):
        self._headers = headers
        self._raises = raises

    def authenticate(self):
        if self._raises:
            raise self._raises
        return self._headers


def test_the_identity_travels_on_authorization():
    """`Authorization` belongs to the Apps proxy, and is the only header the
    ingress wants. Nothing of ours is added beside it."""
    headers = auth_headers(config=FakeConfig({"Authorization": "Bearer abc"}))
    assert headers == {"Authorization": "Bearer abc"}


def test_no_databricks_identity_is_a_normal_state_not_a_failure():
    """A job that cannot authenticate runs unobserved, which is the same state
    as the app being down. Raising here would let "nobody was watching" kill a
    run whose durable path is entirely fine."""
    assert auth_headers(config=FakeConfig(raises=RuntimeError("no creds"))) == {}


def test_an_sdk_that_returns_nothing_is_treated_as_no_identity():
    """Not an empty `Authorization` header. A blank credential presented to the
    proxy is refused the same as none, but reads in a log as though one was
    sent."""
    assert auth_headers(config=FakeConfig(None)) == {}
    assert auth_headers(config=FakeConfig({})) == {}


def test_auth_headers_never_raises_whatever_the_sdk_does():
    for boom in (RuntimeError("x"), ValueError("y"), OSError("z")):
        assert auth_headers(config=FakeConfig(raises=boom)) == {}
