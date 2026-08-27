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

Except when there is no cluster lane installed, which is the same bug one level down: that
gate asked whether the client half worked, and a client-only user who had simply not run
``vast login`` yet failed it -- and was told, fatally, to install kubectl and helm for a
``setup`` command their install does not have.
"""

import pytest

from robovast.client import doctor as doc


@pytest.fixture
def operator_checks(monkeypatch):
    """Pin the operator half so the tests are about fatality, not about this machine.

    The lane is pinned installed along with the rest: `run_checks` consults it to decide
    whether the operator half applies, so leaving it to the real import would make these
    tests pass or fail on whether `robovast-cluster` happens to be in the environment.
    """
    monkeypatch.setattr(doc, "cluster_lane_installed", lambda: True)
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


@pytest.fixture
def no_cluster_lane(monkeypatch):
    """A client-only install: nothing to import, and `check_cluster` reporting that.

    `check_tools` is pinned to *failing* binaries so the tests below are about whether it
    is consulted at all, not about what happens to be on this machine's PATH.
    """
    monkeypatch.setattr(doc, "cluster_lane_installed", lambda: False)
    monkeypatch.setattr(doc, "check_python", lambda: doc.Check("python", True, "3.12"))
    monkeypatch.setattr(doc, "check_cluster", lambda context=None: [
        doc.Check("cluster support", False, "not installed",
                  "Install it to deploy or operate a cluster of your own.",
                  optional=True)])
    monkeypatch.setattr(doc, "check_tools", lambda flavor="": [
        doc.Check("kubectl", False, "not on PATH", "Install kubectl"),
        doc.Check("helm", False, "not on PATH", "Install helm")])


def test_no_lane_and_no_login_does_not_demand_cluster_binaries(
        monkeypatch, no_cluster_lane):
    """The defect: the demotion gate asked the wrong question.

    It asked whether the client half worked, so a client-only user who had simply not run
    `vast login` yet fell through to the fatal branch -- and `helm`'s remedy names `setup`,
    a verb their install does not have. Deploying cannot be the intent when there is
    nothing installed to deploy with, whatever the login says.
    """
    _client(monkeypatch, False)
    checks = doc.run_checks()

    reported = {c.name for c in checks}
    assert not reported & {"kubectl", "helm"}, (
        "a client-only install was asked for the binaries `vast cluster setup` "
        "shells out to, and it has no `setup` to shell out")
    assert {c.name for c in _fatal(checks)} == {"login", "service", "vast on PATH"}, (
        "only the client half may be fatal here -- that is the user's real problem")


def test_the_missing_lane_is_still_reported(monkeypatch, no_cluster_lane):
    """Dropping the binaries must not drop the verdict that explains why they are gone."""
    _client(monkeypatch, False)
    lane = next(c for c in doc.run_checks() if c.name == "cluster support")
    assert lane.status == "warn" and lane.fix, "advisory is not silent, and names a remedy"


def test_python_is_still_checked_without_a_lane(monkeypatch, no_cluster_lane):
    """Needing 3.12 is not the cluster's business, so it survives the lane being absent."""
    _client(monkeypatch, True)
    assert "python" in {c.name for c in doc.run_checks()}


def test_an_installed_lane_still_gets_the_full_operator_half(monkeypatch, operator_checks):
    """The other direction: the fix must not silence an operator who *can* act on it."""
    _client(monkeypatch, False)
    assert {"kubectl", "kubeconfig"} <= {c.name for c in _fatal(doc.run_checks())}


def test_a_failing_optional_client_check_does_not_make_the_operator_half_fatal(
        monkeypatch):
    """`Check.optional` means advisory. One must not decide the operator verdict.

    The operator checks are reported as advisory when the client half is usable, and
    fatal when it is not. Counting an optional client failure as "not usable" turns a
    user whose only problem is advisory -- "this service has no registry configured",
    say -- into four red ✗ for kubectl, helm and a kubeconfig they will never need.

    Latent until something returns one: no client check is optional today, which is why
    the bug sat in `all(c.ok for c in client)` unnoticed.
    """

    monkeypatch.setattr(doc, "check_client", lambda: [
        doc.Check("login", True, "https://svc.example"),
        doc.Check("image builds", False, "unavailable on this service",
                  "run 'vast service upgrade'", optional=True),
    ])
    monkeypatch.setattr(doc, "check_cluster", lambda ctx=None: [
        doc.Check("kubeconfig", False, "no kubeconfig"),
    ])
    monkeypatch.setattr(doc, "check_python", lambda: doc.Check("python", True, "3.12"))
    monkeypatch.setattr(doc, "check_tools", lambda flavor=None: [])

    checks = doc.run_checks()

    operator = [c for c in checks if c.name in ("kubeconfig", "python")]
    assert operator, "the operator checks vanished"
    assert all(c.optional for c in operator), (
        "a failing *optional* client check made the operator half fatal; only a "
        "non-optional client failure should do that")
