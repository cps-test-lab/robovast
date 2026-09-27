# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``vast doctor`` answers for two roles, and must not fail one for the other's needs.

The operator needs python, kubectl, helm, a kubeconfig, RBAC and node capacity to *deploy*
RoboVAST; a user with a URL and a token needs none of them.

The client checks come first, and when they all pass the operator prerequisites drop to
advisory: still listed, still with their remedies, but not a failure. When the client half
is *not* working, deploying is the likely intent and they stay fatal. Without cluster support
installed its checks are not there to report, and one advisory line says why.
"""

import pytest

from robovast.client import doctor as doc


@pytest.fixture
def operator_checks(monkeypatch):
    """Pin the operator half so the tests are about fatality, not about this machine."""
    monkeypatch.setattr(doc, "check_python", lambda: doc.Check("python", True, "3.12"))
    monkeypatch.setattr(doc, "plugin_checks", lambda options: ([
        doc.Check("kubectl", False, "not on PATH", "Install kubectl"),
        doc.Check("kubeconfig", False, "none", "Point kubectl at a cluster")], []))
    monkeypatch.setattr(doc, "check_cluster_support", lambda: [])


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


def test_the_cli_options_reach_the_plugins(monkeypatch):
    """`--flavor`, `-x` and `-n` are what the plugins were asked about."""
    seen = []
    monkeypatch.setattr(doc, "check_client", lambda: [])
    monkeypatch.setattr(doc, "plugin_checks", lambda options: seen.append(options) or ([], []))
    doc.run_checks(flavor="gcp", context="ctx", namespace="ns")
    assert seen == [doc.DoctorOptions(flavor="gcp", context="ctx", namespace="ns")]


@pytest.fixture
def no_cluster(monkeypatch):
    """A client-only install: no plugin contributes anything, and no cluster package."""
    monkeypatch.setattr(doc, "check_python", lambda: doc.Check("python", True, "3.12"))
    monkeypatch.setattr(doc, "plugin_checks", lambda options: ([], []))
    monkeypatch.setattr(doc, "_installed", lambda name: False)


def test_no_cluster_support_and_no_login_fails_only_the_client_half(monkeypatch, no_cluster):
    """Without cluster support installed, deploying cannot be the intent."""
    _client(monkeypatch, False)
    checks = doc.run_checks()
    assert {c.name for c in _fatal(checks)} == {"login", "service", "vast on PATH"}, (
        "only the client half may be fatal here -- that is the user's real problem")


def test_missing_cluster_support_is_still_reported(monkeypatch, no_cluster):
    """No cluster rows must not drop the verdict that explains why they are gone."""
    _client(monkeypatch, False)
    support = next(c for c in doc.run_checks() if c.name == "cluster support")
    assert support.status == "warn" and support.fix, "advisory is not silent, and names a remedy"


def test_python_is_still_checked_without_cluster_support(monkeypatch, no_cluster):
    """Needing 3.12 is not the cluster's business, so it survives cluster support being absent."""
    _client(monkeypatch, True)
    assert "python" in {c.name for c in doc.run_checks()}


def test_a_failing_optional_client_check_does_not_make_the_operator_half_fatal(
        monkeypatch):
    """`Check.optional` means advisory. One must not decide the operator verdict.

    Counting an optional client failure as "not usable" would turn a user whose only
    problem is advisory -- "this service has no registry configured", say -- into red rows
    for kubectl, helm and a kubeconfig they will never need.
    """

    monkeypatch.setattr(doc, "check_client", lambda: [
        doc.Check("login", True, "https://svc.example"),
        doc.Check("image builds", False, "unavailable on this service",
                  "run 'vast service upgrade'", optional=True),
    ])
    monkeypatch.setattr(doc, "plugin_checks", lambda options: (
        [doc.Check("kubeconfig", False, "no kubeconfig")], []))
    monkeypatch.setattr(doc, "check_cluster_support", lambda: [])
    monkeypatch.setattr(doc, "check_python", lambda: doc.Check("python", True, "3.12"))

    checks = doc.run_checks()

    operator = [c for c in checks if c.name in ("kubeconfig", "python")]
    assert operator, "the operator checks vanished"
    assert all(c.optional for c in operator), (
        "a failing *optional* client check made the operator half fatal; only a "
        "non-optional client failure should do that")
