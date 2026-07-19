# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the shared service-target resolver.

There is exactly one way in when no ``--cluster`` is given: auto-detect a service
on the conventional local port. No environment variable, no fallback chain — a
cluster verb that finds nothing errors, while ``ui``/``workspace`` fall back to the
in-process local store.
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


def test_target_options_has_no_service_url():
    @st.target_options
    def cmd(cluster, namespace, context):  # pragma: no cover - only inspected
        pass

    names = {p.name for p in cmd.__click_params__}
    assert names == {"cluster", "namespace", "context"}
    assert "service_url" not in names


def test_cluster_flag_opens_and_closes_port_forward(monkeypatch):
    events = []

    class _Proc:
        pass

    proc = _Proc()
    monkeypatch.setattr(st, "_start_port_forward",
                        lambda ns, ctx, echo=True: events.append(("start", ns, ctx))
                        or (proc, "http://127.0.0.1:5555"))
    monkeypatch.setattr(st, "_stop_port_forward",
                        lambda p: events.append(("stop", p)))

    with st.service_client(cluster=True, namespace="ns", context="local") as (client, label):
        assert isinstance(client, HTTPTransport)
        assert client.base_url == "http://127.0.0.1:5555"
        assert "in-cluster" in label
    assert events == [("start", "ns", "local"), ("stop", proc)]
