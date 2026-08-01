"""Make the whole suite offline by construction, on every machine.

Every test file here injects fake clients, so none of them should ever reach AWS.
But "should" was doing all the work: a test that slipped through to a real API call
still PASSED on a developer laptop, because the laptop has credentials and the call
succeeded. It only failed in CI, which has none -- so `NoCredentialsError` became the
signal, and it arrived long after the commit that caused it.

That happened. test_finops.py's ENV lacked the finops harness ARN override, so
_resolve_harness_arn fell through to a live ssm:GetParameter. Green locally, red in
CI for six consecutive commits.

A test making an unstubbed AWS call is not a CI configuration problem. It is a test
that reads and can WRITE production while claiming to be a unit test -- the finops
driver puts DynamoDB items and publishes SNS. Nothing in this suite should be able
to do that by accident, on anyone's machine.

So: neutralize credentials for every test, and make the socket refuse to open. The
failure is then identical everywhere, immediate, and names the call.
"""
from __future__ import annotations

import socket

import pytest

# Enough to make botocore's chain come up empty rather than fall back to a real
# profile, instance metadata, or SSO cache.
_CREDENTIAL_VARS = (
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
    "AWS_SECURITY_TOKEN", "AWS_PROFILE", "AWS_DEFAULT_PROFILE",
    "AWS_ROLE_ARN", "AWS_WEB_IDENTITY_TOKEN_FILE",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
)


@pytest.fixture(autouse=True)
def no_aws(monkeypatch):
    for var in _CREDENTIAL_VARS:
        monkeypatch.delenv(var, raising=False)
    # Point the config/credential files at nothing so a laptop's ~/.aws is invisible.
    monkeypatch.setenv("AWS_CONFIG_FILE", "/dev/null")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/dev/null")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    # Individual test modules set AWS_REGION themselves; keep a default so a missing
    # region does not masquerade as the offline failure this fixture is about.
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")

    real_connect = socket.socket.connect

    def _refuse(self, address, *a, **kw):
        # Unix sockets and loopback stay open: pytest plugins and coverage use them.
        host = address[0] if isinstance(address, tuple) else ""
        if host in ("127.0.0.1", "::1", "localhost", ""):
            return real_connect(self, address, *a, **kw)
        raise AssertionError(
            f"a test tried to open a network connection to {host}. Every client in "
            "this suite is meant to be injected -- if this is an AWS call, the code "
            "under test is reaching a real API and the fake or the env override is "
            "missing (see _resolve_harness_arn's HARNESS_ARN_<NAME> escape hatch).")

    monkeypatch.setattr(socket.socket, "connect", _refuse)
