"""presign-api trusts the identity rhmp-services' Envoy sets after ext_authz, not a JWT of its own."""

import os
import tempfile

import pytest

# Settings and logging are read at import time. No AUTH_JWT_SECRET is set: the service must start without one.
os.environ.setdefault("LOG_FILE_PATH", os.path.join(tempfile.mkdtemp(), "presign-api.log"))
os.environ.setdefault("S3_BUCKET", "test-bucket")
os.environ.pop("AUTH_JWT_SECRET", None)

from fastapi.testclient import TestClient  # noqa: E402

from presign_api import main  # noqa: E402

client = TestClient(main.app)
MODELS = "/downloader/v1/models"


def test_gateway_identity_is_accepted():
    r = client.get(MODELS, headers={"X-User-Id": "1011782795320050", "X-User-Role": "patient"})
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("headers", [
    {},
    {"Authorization": "Bearer eyJhbGciOiJIUzI1NiJ9.e30.x"},  # a token alone proves nothing here
    {"X-User-Id": "abc"},
    {"X-User-Id": "0"},
    {"X-User-Id": "-5"},
])
def test_no_gateway_identity_is_401(headers, caplog):
    r = client.get(MODELS, headers=headers)
    assert r.status_code == 401 and r.json() == {"detail": "Authentication required"}
    assert "did not come through rhmp Envoy ext_authz" in caplog.text


def test_health_needs_no_identity():
    for path in ("/health", "/downloader/v1/health"):
        r = client.get(path)
        assert r.status_code == 200 and "jwt_configured" not in r.json()["checks"]


def test_no_jwt_settings_left():
    assert not hasattr(main.settings, "auth_jwt_secret")
    assert not hasattr(main, "jwt")
