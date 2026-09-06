# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Reaching the service over a tailnet instead of publishing it.

The point of this route is that it asks for nothing an Ingress asks for: no public DNS
record, no certificate, no address that survives a rebuild. What it must not do is quietly
claim more than it delivers -- it publishes to people, not to the cluster's own kubelet.
"""

import json

import pytest

from robovast.execution.cluster_execution import tailnet_deploy as td


def _configure(monkeypatch, server="https://headscale.example.org", key="tskey-auth-x",
               hostname=None):
    for var, value in ((td.LOGIN_SERVER_ENV, server), (td.AUTHKEY_ENV, key)):
        if value:
            monkeypatch.setenv(var, value)
        else:
            monkeypatch.delenv(var, raising=False)
    if hostname:
        monkeypatch.setenv(td.HOSTNAME_ENV, hostname)
    else:
        monkeypatch.delenv(td.HOSTNAME_ENV, raising=False)


def _by_kind(manifests, kind):
    return next(m for m in manifests if m["kind"] == kind)


def test_nothing_configured_is_the_ordinary_case(monkeypatch):
    """Every deployment that does not use this must be unaffected by its existence."""
    _configure(monkeypatch, server="", key="")

    assert td.configured() is None


def test_which_cluster_is_on_a_tailnet_is_not_decided_by_the_environment(monkeypatch):
    """One .env and two contexts would otherwise publish whichever happened to be current.

    The credential stays in the environment -- a pre-auth key on a command line lands in
    shell history -- but the decision is the flag, so a cluster nobody asked to publish
    never is.
    """
    from unittest import mock
    _configure(monkeypatch)

    with mock.patch.object(td, "remove") as removed:
        assert td.ensure_tailnet(enabled=False) == ""

    assert removed.called, "an unasked cluster is reconciled to having no node"


def test_asking_for_a_tailnet_with_no_credential_is_an_argument_error(monkeypatch):
    """--tailnet with nothing to register with would deploy a node that can never come
    up, so it is refused where the operator can still read the message."""
    _configure(monkeypatch, server="", key="")

    with pytest.raises(ValueError, match=td.LOGIN_SERVER_ENV):
        td.ensure_tailnet(enabled=True)


def test_half_a_tailnet_is_refused_rather_than_half_deployed(monkeypatch):
    """A login server with no key cannot register and a key with no server has nothing to
    register with, so either alone would deploy a node that can never come up."""
    _configure(monkeypatch, server="https://headscale.example.org", key="")
    with pytest.raises(ValueError, match=td.AUTHKEY_ENV):
        td.configured()

    _configure(monkeypatch, server="", key="tskey-auth-x")
    with pytest.raises(ValueError, match=td.LOGIN_SERVER_ENV):
        td.configured()


def test_the_hostname_is_what_users_type_and_has_a_default(monkeypatch):
    _configure(monkeypatch)
    assert td.configured()[2] == td.DEFAULT_HOSTNAME

    _configure(monkeypatch, hostname="rv")
    assert td.configured()[2] == "rv"


def test_it_registers_against_the_operators_own_coordination_server(monkeypatch):
    """The whole reason this is configurable: a self-hosted control plane means no third
    party is trusted with the tailnet."""
    manifests = td.manifests("default", "https://headscale.example.org", "tskey-auth-x",
                             "robovast", "robovast-service.default.svc", 8800)
    env = {e["name"]: e for e in
           _by_kind(manifests, "Deployment")["spec"]["template"]["spec"]
           ["containers"][0]["env"]}

    assert "--login-server=https://headscale.example.org" in env["TS_EXTRA_ARGS"]["value"]
    assert env["TS_AUTHKEY"]["valueFrom"]["secretKeyRef"]["name"] == td.AUTHKEY_SECRET_NAME
    assert "value" not in env["TS_AUTHKEY"], "the key must not be inlined in the pod spec"


def test_it_needs_no_privilege_so_it_deploys_on_a_managed_cluster():
    """The alternative wants NET_ADMIN and /dev/net/tun, which a managed cluster refuses --
    and would make this the second privileged thing RoboVAST runs."""
    spec = _by_kind(td.manifests("default", "https://h.example.org", "k", "robovast",
                                 "svc", 8800), "Deployment")["spec"]["template"]["spec"]
    container = spec["containers"][0]
    env = {e["name"]: e.get("value") for e in container["env"]}

    assert env["TS_USERSPACE"] == "true"
    assert "securityContext" not in container
    assert not any(v.get("hostPath") for v in spec["volumes"])


def test_the_identity_survives_a_restart_rather_than_registering_a_second_node():
    """A node that re-registered on every roll would fill the operator's coordination
    server with dead entries, and the name users type would silently become 'robovast-1'."""
    manifests = td.manifests("default", "https://h.example.org", "k", "robovast",
                             "svc", 8800)
    env = {e["name"]: e.get("value") for e in
           _by_kind(manifests, "Deployment")["spec"]["template"]["spec"]
           ["containers"][0]["env"]}
    role = _by_kind(manifests, "Role")

    assert env["TS_KUBE_SECRET"] == td.STATE_SECRET_NAME
    named = [r for r in role["rules"] if r.get("resourceNames")]
    assert named and named[0]["resourceNames"] == [td.STATE_SECRET_NAME], (
        "a container that talks to an outside coordination server gets one Secret by name")


def test_it_proxies_to_the_service_over_plain_http():
    """The transport is already WireGuard, so a certificate would encrypt what is
    encrypted -- and the session cookie's Secure flag follows the scheme, so a browser
    keeps it over http:// here."""
    config = json.loads(td.serve_config("robovast-service.default.svc", 8800))

    assert "80" in config["TCP"]
    handler = config["Web"]["${TS_CERT_DOMAIN}:80"]["Handlers"]["/"]
    assert handler["Proxy"] == "http://robovast-service.default.svc:8800"


def test_one_replica_because_two_would_claim_one_name():
    assert _by_kind(td.manifests("default", "https://h.example.org", "k", "robovast",
                                 "svc", 8800), "Deployment")["spec"]["replicas"] == 1
