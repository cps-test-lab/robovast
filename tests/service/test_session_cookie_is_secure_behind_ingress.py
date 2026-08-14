# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The session cookie must carry ``Secure`` wherever the user speaks HTTPS.

The cookie's value **is** the shared token, so the flag is not hardening -- it is the
difference between the secret staying on the wire it was meant for and being sent in
clear text.

Observed live on the first published deployment, which every other check passed:

    set-cookie: robovast_session=<token>; HttpOnly; Path=/; SameSite=strict

No ``Secure``. TLS ends at the ingress controller, which forwards plain HTTP to the pod;
uvicorn trusts ``X-Forwarded-Proto`` only from 127.0.0.1 and the controller comes from a
cluster IP, so the app concluded it was serving http and dropped the flag. The Ingress'
308 from port 80 does not cover it -- a browser attaches the cookie to the http request
and only then learns to upgrade.

The whole deployment insists on TLS *because* the cookie is Secure (that is what
``--insecure-http`` exists to override), so the one guarantee the interlock was built to
protect was the one silently missing.
"""

from unittest.mock import patch

import pytest

from robovast.service.app import _proxy_trust


# -- the decision ---------------------------------------------------------------------
def test_a_pod_trusts_the_ingress_that_fronts_it():
    proxy_headers, allow = _proxy_trust({"KUBERNETES_SERVICE_HOST": "10.43.0.1"})
    assert proxy_headers is True
    assert allow == "*", (
        "the ingress controller's pod IP is not knowable in advance, and the port is "
        "only reachable through the Service anyway")


def test_a_developers_local_serve_trusts_nobody():
    """`vast serve` on a laptop is plain http, and its cookie must stay non-Secure.

    Setting Secure there would make the dev login silently fail to persist -- the
    opposite bug, and a much more confusing one.
    """
    proxy_headers, allow = _proxy_trust({})
    assert proxy_headers is False
    assert allow == "127.0.0.1"


def test_serve_passes_the_decision_to_uvicorn():
    """The helper is only worth anything if `serve` actually uses it."""
    import robovast.service.app as app_module
    source = __import__("inspect").getsource(app_module.serve)
    assert "_proxy_trust(os.environ)" in source
    assert "proxy_headers=proxy_headers" in source
    assert "forwarded_allow_ips=forwarded_allow_ips" in source


# -- the resulting cookie -------------------------------------------------------------
@pytest.mark.parametrize("scheme, expect_secure", [("https", True), ("http", False)])
def test_the_cookie_follows_the_scheme_the_user_spoke(scheme, expect_secure):
    """With the headers trusted, `request.url.scheme` is the user's scheme, not the hop's.

    Both directions matter: ``--insecure-http`` is a supported (if discouraged) mode, and
    there the flag must stay off or the login cannot work at all.
    """
    from fastapi.testclient import TestClient

    from robovast.service import auth
    # Exercise the real cookie-setting code path rather than a copy of it.
    from robovast.service.app import build_app

    token = "s3cret-token-for-the-test"

    class _Impl:
        def __getattr__(self, _name):
            raise AssertionError("the login route must not touch the backend")

    with patch("robovast.service.app._mount_ui"):
        real = build_app(_Impl(), mount_mcp=False, auth_token=token)

    client = TestClient(real, base_url=f"{scheme}://robovast.example.org")
    response = client.post("/login", data={"token": token, "name": "t"},
                           follow_redirects=False)
    assert response.status_code == 303

    raw = response.headers.get_list("set-cookie")
    session = next(c for c in raw if c.startswith(auth.SESSION_COOKIE))
    assert ("Secure" in session) is expect_secure, session
    # HttpOnly is not the thing under test, but losing it here would be just as bad.
    assert "HttpOnly" in session
