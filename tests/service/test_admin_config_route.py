# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``GET /admin/config`` -- the service reporting what it is configured with.

The report itself is covered in ``tests/common/test_settings_report.py``; what is worth
pinning HERE is the wiring the route adds and could lose in a refactor: the shared secret
gates it like every other route, and the loopback rule that decides whether host paths are
publishable is actually applied to the request rather than merely available.
"""

import pytest
from fastapi.testclient import TestClient

from robovast.service import settings_report
from robovast.service.app import build_app
from robovast.service.interface import Routes, VersionInfo

TOKEN = "t"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class _Service:
    def version(self):
        return VersionInfo(robovast_version="x", backend="docker")

    def __getattr__(self, name):
        raise AttributeError(name)


@pytest.fixture
def app(monkeypatch):
    import os
    for key in [k for k in os.environ if k.startswith(settings_report.PREFIX)]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(settings_report, "GIT_TOKEN_MOUNT", "/nonexistent/robovast-git")
    monkeypatch.setenv("ROBOVAST_WORKSPACES_ROOT", "/srv/robovast/workspaces")
    monkeypatch.setenv("ROBOVAST_NTFY_TOPIC", "campaigns")
    monkeypatch.setenv("ROBOVAST_AUTH_TOKEN", "the-shared-secret")
    return build_app(_Service(), mount_mcp=False, auth_token=TOKEN)


def _settings(response) -> dict:
    return {row["key"]: row for row in response.json()["settings"]}


def test_the_route_needs_the_shared_secret(app):
    """It reports how the service is set up; it is not a public fact about it."""
    assert TestClient(app, headers={}).get(Routes.ADMIN_CONFIG).status_code == 401


def test_an_authenticated_caller_gets_the_settings(app):
    client = TestClient(app, client=("127.0.0.1", 5000))
    response = client.get(Routes.ADMIN_CONFIG, headers=AUTH)

    assert response.status_code == 200
    rows = _settings(response)
    assert rows["ROBOVAST_NTFY_TOPIC"]["value"] == "campaigns"
    assert response.json()["how_to_change"]


def test_the_configured_token_is_never_in_the_payload(app):
    """The service's own access token is a setting like any other -- and a credential."""
    client = TestClient(app, client=("127.0.0.1", 5000))
    response = client.get(Routes.ADMIN_CONFIG, headers=AUTH)

    assert "the-shared-secret" not in response.text
    assert _settings(response)["ROBOVAST_AUTH_TOKEN"]["is_set"] is True


def test_host_paths_are_blanked_for_a_remote_caller(app):
    """The loopback rule reaches the request, rather than only existing in the module."""
    local = _settings(TestClient(app, client=("127.0.0.1", 5000))
                      .get(Routes.ADMIN_CONFIG, headers=AUTH))
    remote = _settings(TestClient(app, base_url="http://testserver")
                       .get(Routes.ADMIN_CONFIG, headers=AUTH))

    assert local["ROBOVAST_WORKSPACES_ROOT"]["value"] == "/srv/robovast/workspaces"
    assert remote["ROBOVAST_WORKSPACES_ROOT"]["value"] is None
    assert remote["ROBOVAST_WORKSPACES_ROOT"]["withheld"] == "host_path"
