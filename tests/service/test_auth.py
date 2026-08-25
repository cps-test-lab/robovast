# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The service is not reachable without the shared secret, and there is no way off that.

The properties worth pinning are the ones a plausible refactor would break:

* a token is *minted* when none is configured, rather than the check being skipped —
  there is no unauthenticated mode to fall into by forgetting an environment variable;
* the comparison is constant-time;
* a browser gets a login page and a cookie, an API client gets 401 and a bearer
  challenge — because ``EventSource`` cannot send headers, so the cookie is what keeps
  every live stream in the web UI working;
* ``/healthz`` stays open, or the kubelet restarts the pod forever.
"""

import inspect

import pytest
from starlette.testclient import TestClient

from robovast.service import auth
from robovast.service.app import build_app
from robovast.service.client import LocalTransport
from robovast.service.interface import Routes

TOKEN = "correct-horse-battery-staple"


@pytest.fixture(name="client")
def _client():
    # Explicit empty headers: the suite's conftest would otherwise authenticate every
    # request, which is exactly what these tests must be able to *not* do.
    with TestClient(build_app(LocalTransport(), mount_mcp=False, auth_token=TOKEN),
                    headers={}) as client:
        yield client


def _auth(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


def test_healthz_is_reachable_without_a_token(client):
    """The kubelet has no credential; a gated probe would restart the pod forever."""
    assert client.get(Routes.HEALTHZ).status_code == 200


@pytest.mark.parametrize("headers", [
    pytest.param({}, id="no-token"),
    pytest.param({"Authorization": "Bearer wrong"}, id="wrong-token"),
    pytest.param({"Authorization": TOKEN}, id="token-without-bearer-scheme"),
])
def test_an_api_request_without_a_valid_token_is_401(client, headers):
    response = client.get(Routes.VERSION, headers=headers)
    assert response.status_code == 401
    assert "bearer" in response.headers.get("www-authenticate", "").lower()


def test_a_valid_bearer_token_is_accepted(client):
    assert client.get(Routes.VERSION, headers=_auth()).status_code == 200


def test_a_browser_navigation_is_sent_to_the_login_page(client):
    """A person typing the URL should meet a password box, not a JSON parse error."""
    response = client.get(Routes.VERSION, headers={"Accept": "text/html"},
                          follow_redirects=False)
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith(Routes.LOGIN)
    # The original target survives, so login returns you where you were going.
    assert "next=%2Fversion" in location


def test_logging_in_sets_a_session_cookie_that_authenticates(client):
    response = client.post(Routes.LOGIN, data={"token": TOKEN, "next": "/"},
                           follow_redirects=False)
    assert response.status_code == 303
    assert auth.SESSION_COOKIE in response.cookies
    # No Authorization header from here on: the cookie alone must carry the session,
    # which is the property EventSource depends on.
    assert client.get(Routes.VERSION).status_code == 200


def test_a_wrong_token_on_the_login_form_is_rejected(client):
    response = client.post(Routes.LOGIN, data={"token": "nope", "next": "/"},
                           follow_redirects=False)
    assert response.status_code == 401
    assert auth.SESSION_COOKIE not in response.cookies


def test_the_session_cookie_is_http_only_and_the_name_cookie_is_not(client):
    """The token must be unreadable by scripts; the display name is meant to be read."""
    response = client.post(Routes.LOGIN, data={"token": TOKEN, "name": "Fred", "next": "/"},
                           follow_redirects=False)
    cookies = response.headers.get_list("set-cookie")
    session = next(c for c in cookies if c.startswith(auth.SESSION_COOKIE))
    name = next(c for c in cookies if c.startswith(auth.NAME_COOKIE))
    assert "HttpOnly" in session
    assert "HttpOnly" not in name
    assert "SameSite=strict" in session.lower().replace("samesite=strict", "SameSite=strict")


@pytest.mark.real_auth
def test_there_is_no_unauthenticated_mode(monkeypatch):
    """An unset token is minted, never treated as "no authentication required"."""
    monkeypatch.delenv(auth.TOKEN_ENV_VAR, raising=False)
    token, ephemeral = auth.resolve_token(None)
    assert token and ephemeral
    with TestClient(build_app(LocalTransport(), mount_mcp=False), headers={}) as client:
        assert client.get(Routes.VERSION).status_code == 401


@pytest.mark.real_auth
def test_a_configured_token_is_used_as_is(monkeypatch):
    monkeypatch.setenv(auth.TOKEN_ENV_VAR, "from-the-environment")
    assert auth.resolve_token(None) == ("from-the-environment", False)


def test_token_comparison_is_constant_time():
    """`==` on a secret leaks its prefix through timing; this must not regress."""
    source = inspect.getsource(auth._token_matches)
    assert "compare_digest" in source


def test_the_principal_records_a_self_declared_name():
    principal = auth.principal_from_headers(
        {"authorization": f"Bearer {TOKEN}", "x-robovast-user": "Fred"}, TOKEN)
    assert principal.authenticated
    assert principal.display_name == "Fred"
    assert principal.source == "shared-secret"


def test_a_missing_name_stays_missing():
    """"Nobody said" and "someone called themselves X" are different facts."""
    principal = auth.principal_from_headers({"authorization": f"Bearer {TOKEN}"}, TOKEN)
    assert principal.authenticated
    assert principal.display_name is None


def test_an_unauthenticated_principal_is_not_trusted_with_a_name():
    principal = auth.principal_from_headers({"x-robovast-user": "Fred"}, TOKEN)
    assert not principal.authenticated
