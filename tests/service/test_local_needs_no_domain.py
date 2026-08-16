# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A local service needs no hostname, no domain and no certificate.

That is what makes ``vast serve`` a two-command evaluation rather than a DNS and TLS
exercise, and it is easy to lose one requirement at a time: a cookie that insists on
``Secure``, a redirect built from a configured hostname, a check that refuses plain HTTP.
Each would be individually defensible and would collectively make a local service
unusable without a domain.

The refusals that *do* demand TLS and a token are deliberate, and belong to the cluster
half -- ``vast exec cluster setup`` publishes to other machines, where the token really
would cross a network in clear text. This file pins the boundary between the two, not the
absence of the checks.
"""

# pylint: disable=redefined-outer-name  # the pytest fixture idiom

import re

import pytest
from starlette.testclient import TestClient

from robovast.service.app import build_app
from robovast.service.interface import DEFAULT_PORT


def test_serve_binds_loopback_on_the_conventional_port_by_default():
    """No hostname anywhere in the default invocation."""
    from robovast.common.cli.core_commands import serve

    opts = {p.name: p for p in serve.params}
    assert opts["host"].default == "127.0.0.1"
    assert opts["port"].default is DEFAULT_PORT, (
        "the port must be the shared constant, not a second copy of 8800 -- a client "
        "probes that constant to find a local service")


def test_the_port_is_not_declared_twice():
    """It was, once: `service/app.py` and the cluster deploy manifests each had an 8800,
    and a client probing for a local service read the *cluster* one."""
    from robovast.execution.cluster_execution.service_deploy import SERVICE_PORT
    from robovast.service.app import DEFAULT_PORT as served

    assert DEFAULT_PORT is served is SERVICE_PORT


def test_a_login_over_plain_http_sets_a_usable_cookie(local_service):
    """The property a browser login on ``http://127.0.0.1`` depends on.

    A cookie marked ``Secure`` is not sent back over HTTP, so hard-coding that flag would
    let the login appear to succeed and then drop the session on the next request -- the
    failure would look like a broken service rather than a wrong cookie.
    """
    client, token = local_service
    resp = client.post("/login", data={"token": token}, follow_redirects=False)

    assert resp.status_code < 400, resp.text
    cookies = resp.headers.get_list("set-cookie")
    assert cookies, "login set no cookie at all"
    assert not any("secure" in c.lower() for c in cookies), (
        f"a Secure cookie is never returned over http, so the session would be lost: "
        f"{cookies}")


def test_the_same_login_over_https_does_mark_the_cookie_secure(local_service):
    """The flag is conditional, not absent. Behind a TLS-terminating proxy it must be
    set, or the token would be sent back over any later plain-HTTP request."""
    client, token = local_service
    resp = client.post("https://testserver/login", data={"token": token},
                       follow_redirects=False)

    assert resp.status_code < 400, resp.text
    cookies = resp.headers.get_list("set-cookie")
    assert any("secure" in c.lower() for c in cookies), cookies


def test_nothing_in_the_local_path_demands_tls_or_a_hostname():
    """The refusals exist -- they just belong to the cluster half.

    Checked by where they live rather than by calling them: `vast serve` has no code path
    to a hostname requirement at all, and the way that stays true is that these checks
    stay in the setup module a local service never imports.
    """
    from robovast.execution.cluster_execution import cluster_setup

    source = __import__("inspect").getsource(cluster_setup)
    assert re.search(r"ingress", source, re.I), (
        "the TLS/ingress refusals moved out of cluster_setup; if they are now on a path "
        "`vast serve` can reach, a local service has gained a hostname requirement")


@pytest.fixture
def local_service(monkeypatch, tmp_path):
    """A service as `vast serve` would build it locally: plain HTTP, loopback."""
    monkeypatch.setenv("ROBOVAST_WORKSPACES_ROOT", str(tmp_path / "ws"))

    class _Impl:
        """Enough interface for the login route; nothing here executes anything."""

        def __getattr__(self, name):
            raise AttributeError(name)

    # Passed, not set in the environment: build_app *mints* a token when it is not
    # given, so there is no way to construct an unauthenticated app -- which is the
    # property "no domain needed" must not be confused with.
    app = build_app(_Impl(), mount_mcp=False, auth_token="tok-local")
    return TestClient(app, base_url="http://testserver"), "tok-local"
