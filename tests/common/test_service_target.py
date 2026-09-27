# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the shared service-target resolver.

Two ways in, in order: a service answering on the conventional local port, then the one
``vast login`` stored. Finding neither is an error for every command: the client is a
frontend, so there is no in-process store to fall back to.

*One* way in is enough only while the sole way to reach a remote service is a tunnel to
the local port; it cannot express "the service is at https://robovast.example.org and
here is my token", which is what a user with no kubeconfig needs. What is still asserted
is the narrowness that matters: no ambient environment variable names a service, and the
resolution is announced rather than silent.
"""

import click
import pytest

from robovast.client import service_target as st
from robovast.service.client import HTTPTransport


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


def test_service_client_raises_when_no_service_answers(monkeypatch):
    """No service is a missing dependency, for every verb -- not a second implementation.

    Yielding an in-process transport unless the caller passes ``require_service=True`` has
    ``workspace init`` writing into a local store with nothing listening while ``workspace
    run`` refuses: one command name, two systems, chosen by what happens to be on the port.
    There is no such parameter to default, so a caller cannot ask for that back.
    """
    monkeypatch.setattr(st, "_service_alive", lambda url: False)
    with pytest.raises(click.ClickException, match="No robovast-service found"):
        with st.service_client():
            pass


def test_service_client_has_no_serviceless_switch():
    """No keyword re-opens the in-process path -- the check a reader would otherwise
    have to do by reading every call site."""
    import inspect
    assert "require_service" not in inspect.signature(st.service_client).parameters


def _verbs(group, path=()):
    ctx = click.Context(group)
    for name in group.list_commands(ctx):
        cmd = group.get_command(ctx, name)
        if isinstance(cmd, click.Group):
            yield from _verbs(cmd, path + (name,))
        elif cmd is not None:
            yield " ".join(path + (name,)), cmd


#: Verbs that drive Kubernetes themselves, and so read a context and a namespace.
_KUBE_MODULE = "robovast.execution.cluster_execution.cli"


def test_only_verbs_that_drive_a_cluster_take_a_kube_context():
    """A verb that only talks to the service offers no ``--context``/``--namespace``.

    The service is resolved (local port, then ``vast login``), never named per call, so
    such a flag on a service verb would be accepted and ignored. Only the cluster
    distribution's verbs and ``doctor`` read one.
    """
    from robovast.client.cli import cli, load_plugins
    load_plugins()
    offering = {path: cmd.callback.__module__ for path, cmd in _verbs(cli)
                if {p.name for p in cmd.params} & {"namespace", "context", "kube_context"}}
    assert offering, "no verb offers a kube context at all -- the walk found nothing"
    stray = {path: mod for path, mod in offering.items()
             if mod != _KUBE_MODULE and path != "doctor"}
    assert not stray, f"service verbs offering a kube context they ignore: {stray}"


@pytest.mark.parametrize("argv", [
    ["workspace", "run", "--context", "somewhere", "ws"],
    ["campaign", "list", "-n", "elsewhere"],
    ["files", "ls", "-x", "somewhere", "/sources"],
])
def test_a_service_verb_refuses_a_kube_context(argv):
    from click.testing import CliRunner

    from robovast.client.cli import cli, load_plugins
    load_plugins()
    result = CliRunner().invoke(cli, argv)
    assert result.exit_code == 2, result.output
    assert "No such option" in result.output


def test_no_tunnel_is_opened_for_a_command(monkeypatch):
    """The port-forward helpers are gone; reaching the service opens nothing."""
    assert not hasattr(st, "_start_port_forward")
    assert not hasattr(st, "_stop_port_forward")

    monkeypatch.setattr(st, "_service_alive", lambda url: True)
    with st.service_client() as (client, label):
        assert isinstance(client, HTTPTransport)
        assert client.base_url == "http://127.0.0.1:8800"
        assert "detected" in label
