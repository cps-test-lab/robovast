# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the shared service-target resolver.

Two ways in, in order: a service answering on the conventional local port, then the one
``vast login`` stored. A cluster verb that finds neither errors, while
``ui``/``workspace`` fall back to the in-process local store.

This used to be *one* way in, with the narrowness itself asserted below. That was right
while the only way to reach a remote service was a tunnel to the local port; it could
not express "the service is at https://robovast.example.org and here is my token", which
is what a user with no kubeconfig needs. What is still asserted is the part that
mattered: no ambient environment variable names a service, and the resolution is
announced rather than silent.
"""

import click
import pytest

from robovast.common.cli import service_target as st
from robovast.service.client import HTTPTransport, LocalTransport


def test_detected_service_url_probes_conventional_port(monkeypatch):
    seen = {}

    def fake_alive(url):
        seen["url"] = url
        return True

    monkeypatch.setattr(st, "_service_alive", fake_alive)
    url = st.detected_service_url()
    assert url == "http://127.0.0.1:8800"
    assert seen["url"] == "http://127.0.0.1:8800"


def test_detected_service_url_empty_when_nothing_answers(monkeypatch):
    monkeypatch.setattr(st, "_service_alive", lambda url: False)
    assert st.detected_service_url() == ""


def test_service_client_follows_detected_service(monkeypatch):
    monkeypatch.setattr(st, "_service_alive", lambda url: True)
    with st.service_client() as (client, label):
        assert isinstance(client, HTTPTransport)
        assert client.base_url == "http://127.0.0.1:8800"
        assert "detected" in label


def test_service_client_local_when_no_service(monkeypatch):
    monkeypatch.setattr(st, "_service_alive", lambda url: False)
    with st.service_client() as (client, label):
        assert isinstance(client, LocalTransport)
        assert label.startswith("this machine")


def test_service_client_require_service_raises_when_none(monkeypatch):
    monkeypatch.setattr(st, "_service_alive", lambda url: False)
    with pytest.raises(click.ClickException, match="No robovast-service found"):
        with st.service_client(require_service=True):
            pass


def test_target_options_names_no_service():
    """No flag and no environment variable picks a service; a login does.

    The original of this test asserted the option set was exactly
    ``{cluster, namespace, context}``. ``--cluster`` is gone with the tunnel it drove,
    and the remaining two are for the Kubernetes work a command does itself — neither
    selects which service answers.
    """
    @st.target_options
    def cmd(namespace, context):  # pragma: no cover - only inspected
        pass

    names = {p.name for p in cmd.__click_params__}
    assert names == {"namespace", "context"}
    assert "service_url" not in names
    assert "cluster" not in names


def test_no_tunnel_is_opened_for_a_command(monkeypatch):
    """The port-forward helpers are gone; reaching the service opens nothing."""
    assert not hasattr(st, "_start_port_forward")
    assert not hasattr(st, "_stop_port_forward")

    monkeypatch.setattr(st, "_service_alive", lambda url: True)
    with st.service_client(namespace="ns", context="local") as (client, label):
        assert isinstance(client, HTTPTransport)
        assert client.base_url == "http://127.0.0.1:8800"
        assert "detected" in label
