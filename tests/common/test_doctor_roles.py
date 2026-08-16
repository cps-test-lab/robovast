# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``vast doctor`` answers for two roles, and must not fail one for the other's needs.

It was written for the operator: python, kubectl, helm, a kubeconfig, RBAC, node
capacity. All of those are what you need to *deploy* RoboVAST. A user with a URL and a
token needs none of them — and got four FAILs and a non-zero exit telling them so, which
is the opposite of useful for the one person who had nothing left to do.

The client checks now come first, and when they all pass the operator prerequisites drop
to advisory: still listed, still with their remedies, but not a failure. When the client
half is *not* working, deploying is the likely intent and they stay fatal.
"""

import pytest

from robovast.common.cli import doctor as doc


@pytest.fixture
def operator_checks(monkeypatch):
    """Pin the operator half so the tests are about fatality, not about this machine."""
    monkeypatch.setattr(doc, "check_python", lambda: doc.Check("python", True, "3.12"))
    monkeypatch.setattr(doc, "check_tools", lambda flavor="": [
        doc.Check("kubectl", False, "not on PATH", "Install kubectl")])
    monkeypatch.setattr(doc, "check_cluster", lambda context=None: [
        doc.Check("kubeconfig", False, "none", "Point kubectl at a cluster")])


def _client(monkeypatch, ok: bool):
    checks = [doc.Check("login", ok, "u"), doc.Check("service", ok, "u"),
              doc.Check("vast on PATH", ok, "/usr/bin/vast")]
    monkeypatch.setattr(doc, "check_client", lambda: checks)


def _fatal(checks):
    return [c for c in checks if not c.ok and not c.optional]


def test_a_working_client_makes_the_operator_prerequisites_advisory(
        monkeypatch, operator_checks):
    _client(monkeypatch, True)
    checks = doc.run_checks()
    assert not _fatal(checks), "a user with a working login is not broken"
    assert [c.name for c in checks if not c.ok] == ["kubectl", "kubeconfig"], \
        "they are still reported — advisory is not silent"


def test_they_keep_their_remedies_when_advisory(monkeypatch, operator_checks):
    _client(monkeypatch, True)
    kubectl = next(c for c in doc.run_checks() if c.name == "kubectl")
    assert kubectl.status == "warn" and kubectl.fix == "Install kubectl"


def test_a_broken_client_leaves_them_fatal(monkeypatch, operator_checks):
    """Nothing usable yet: deploying is the likely intent, and a missing helm stops it."""
    _client(monkeypatch, False)
    assert {c.name for c in _fatal(doc.run_checks())} >= {"kubectl", "kubeconfig"}


def test_the_client_checks_come_first(monkeypatch, operator_checks):
    """Order is the message: what you need to *use* it, before what you need to ship it."""
    _client(monkeypatch, True)
    names = [c.name for c in doc.run_checks()]
    assert names[:3] == ["login", "service", "vast on PATH"]


def test_a_client_failure_is_always_fatal(monkeypatch, operator_checks):
    """Whatever the operator half says: without these you cannot reach a service."""
    _client(monkeypatch, False)
    assert {"login", "service", "vast on PATH"} <= {c.name for c in _fatal(doc.run_checks())}
