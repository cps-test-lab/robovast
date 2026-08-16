# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Client-wide policy applied when Kubernetes configuration is loaded."""

from unittest import mock

import pytest

from robovast.execution.cluster_execution import kube_client as kube


@pytest.fixture
def installed(monkeypatch):
    """Install the connect-timeout wrapper on a recording request method.

    ``monkeypatch`` restores the real ``RESTClientObject.request`` afterwards, so the
    process-wide patch does not leak into other tests.
    """
    from kubernetes.client import rest

    seen = []
    monkeypatch.setattr(rest.RESTClientObject, "request",
                        lambda self, *a, **kw: seen.append(kw.get("_request_timeout")))
    monkeypatch.setattr(kube, "_connect_timeout_installed", False)
    kube._install_default_connect_timeout()
    return rest.RESTClientObject.request, seen


def test_api_calls_get_a_connect_timeout(installed):
    """A connect to a cluster that is not there (stopped, VPN down) used to block for the
    OS TCP timeout on every retry — minutes before the failure was even reported. The
    generated client passes timeout=None to urllib3, overriding any pool default, so the
    default has to be injected here."""
    request, seen = installed

    request(object(), "GET", "/api/v1/namespaces")

    assert seen == [(kube.CONNECT_TIMEOUT_SECONDS, None)]  # read stays unlimited


def test_explicit_request_timeout_is_not_overridden(installed):
    """A caller that asked for a specific timeout gets it — the injection is a default,
    not a policy imposed over the argument."""
    request, seen = installed

    request(object(), "GET", "/api/v1/namespaces", _request_timeout=(1, 2))

    assert seen == [(1, 2)]


def test_loading_config_installs_the_timeout(monkeypatch):
    """load_kube_config is the one entry point every cluster path goes through, so the
    policy is installed there rather than at each call site."""
    monkeypatch.setattr(kube, "_connect_timeout_installed", False)
    install = mock.Mock()
    monkeypatch.setattr(kube, "_install_default_connect_timeout", install)
    with mock.patch("kubernetes.config.load_incluster_config"):
        kube.load_kube_config()
    install.assert_called_once()
