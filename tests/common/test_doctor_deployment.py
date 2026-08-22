# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``vast doctor`` says whether a deployment can build, and which remedy applies.

The failure this exists for: a campaign refused at submit with "nowhere to push it", after
a project push, a workspace create and a launch — while ``vast doctor`` reported all-green
throughout, because its cluster checks covered kubeconfig, API reachability, RBAC and node
capacity and never looked at the deployed service.

Worse, the refusal named one remedy. ``setup --ingress-host`` is the fix for *never
published*; the deployment in question **was** published and had merely lost its prefix,
where ``upgrade`` recovers it. The in-pod service cannot tell those apart — it reads the
prefix out of its environment and has no RBAC to read its own Ingress. From a machine with
a kubeconfig they *are* distinguishable, which is the whole reason this check lives here.

Two names on purpose: ``build registry`` and ``registry route`` describe the
infrastructure, while the client-side ``image builds`` reports what the running service
says it can do. They can legitimately disagree — a pod predating its own config — and two
rows sharing one name would read as a single check contradicting itself.
"""

# pylint: disable=redefined-outer-name  # the pytest fixture idiom
# pylint: disable=protected-access  # _check_build_capability is what this file tests

import types
from unittest.mock import MagicMock, patch

import pytest

from robovast.client import doctor as doc


@pytest.fixture
def deployment(monkeypatch):
    """Stub the `service_deploy` reads `check_deployment` makes, by name."""
    from robovast.execution.cluster_execution import service_deploy

    state = types.SimpleNamespace(config="rke2", prefix="", host="", defects=[])

    monkeypatch.setattr(service_deploy, "read_service_config_from_cluster",
                        lambda ns, ctx: (state.config, {}) if state.config else (None, {}))
    monkeypatch.setattr(service_deploy, "deployed_registry_prefix",
                        lambda ns, ctx: state.prefix)
    monkeypatch.setattr(service_deploy, "published_host", lambda ns, ctx: state.host)
    monkeypatch.setattr(service_deploy, "registry_ingress_defects",
                        lambda ingress: state.defects)
    # The Ingress read itself; `_check_registry_route` only needs it not to raise.
    monkeypatch.setattr("kubernetes.client.NetworkingV1Api",
                        lambda *a, **k: MagicMock(
                            read_namespaced_ingress=lambda *_a, **_k: object()))
    return state


def _by_name(checks, name):
    found = [c for c in checks if c.name == name]
    assert found, f"no {name!r} check in {[c.name for c in checks]}"
    return found[0]


def test_no_deployment_in_this_namespace_says_so_and_suggests_n(deployment):
    """The commonest mistake is looking in the wrong namespace, not a broken cluster."""
    deployment.config = None

    check = _by_name(doc.check_deployment(namespace="nope"), "build registry")
    assert check.ok is False
    assert "nope" in check.detail
    assert "-n" in check.fix, "must name the flag that looks elsewhere"
    assert check.optional


def test_a_configured_prefix_is_reported(deployment):
    deployment.config, deployment.prefix = "rke2", "robovast.example.org"

    assert _by_name(doc.check_deployment(), "build registry").ok is True


def test_published_but_no_prefix_names_upgrade(deployment):
    """The state that actually happened, and the remedy the refusal got wrong."""
    deployment.config, deployment.prefix = "rke2", ""
    deployment.host = "robovast.example.org"

    check = _by_name(doc.check_deployment(), "build registry")
    assert check.ok is False
    assert "published at robovast.example.org" in check.detail
    assert "vast exec cluster upgrade" in check.fix
    assert "setup" not in check.fix, (
        "with the Ingress readable, this check knows which remedy applies -- offering "
        "both here would put the ambiguity back that it exists to resolve")


def test_not_published_names_setup_with_its_tls_options(deployment):
    deployment.config, deployment.prefix, deployment.host = "rke2", "", ""

    check = _by_name(doc.check_deployment(), "build registry")
    assert check.ok is False
    assert "vast exec cluster setup" in check.fix
    assert "--ingress-host" in check.fix
    assert "upgrade" not in check.fix
    # Publishing over plain HTTP is refused, so naming --ingress-host alone would send the
    # operator to a command that then refuses.
    assert "--issuer" in check.fix or "--tls-secret" in check.fix


def test_a_broken_route_is_reported_only_once_there_is_a_registry(deployment):
    """"the route is broken" is noise when there is nothing to route to."""
    deployment.config, deployment.prefix = "rke2", "robovast.example.org"
    deployment.defects = ["no /v2 route to the registry"]

    checks = doc.check_deployment()
    route = _by_name(checks, "registry route")
    assert route.ok is False
    assert "/v2" in route.detail
    assert "vast exec cluster upgrade" in route.fix

    deployment.prefix = ""
    assert not [c for c in doc.check_deployment() if c.name == "registry route"], (
        "no registry means no route check")


def test_an_unusable_cluster_says_nothing(monkeypatch):
    """`check_cluster` has already reported it; twice makes a reader chase two problems."""
    from robovast.execution.cluster_execution import service_deploy

    monkeypatch.setattr(service_deploy, "read_service_config_from_cluster",
                        MagicMock(side_effect=RuntimeError("unreachable")))

    assert doc.check_deployment() == []


def test_it_is_not_reached_when_the_cluster_checks_fail(monkeypatch):
    """The gate in `run_checks`. Asking a deployment about itself over a dead API server
    is a second way of saying "no cluster"."""
    monkeypatch.setattr(doc, "check_client", lambda: [doc.Check("login", True, "url")])
    monkeypatch.setattr(doc, "check_python", lambda: doc.Check("python", True, "3.12"))
    monkeypatch.setattr(doc, "check_tools", lambda flavor=None: [])
    monkeypatch.setattr(doc, "check_cluster",
                        lambda ctx=None: [doc.Check("kubeconfig", False, "none")])

    def _must_not_run(*_a, **_k):
        raise AssertionError("check_deployment ran despite an unusable cluster")

    monkeypatch.setattr(doc, "check_deployment", _must_not_run)
    doc.run_checks()


# -- the client side, which needs no kubectl ---------------------------------


def _handshake(version=None, raises=None):
    """The service's VersionInfo as `check_client` reads it, with the HTTP call stubbed."""
    client = MagicMock()
    if raises is not None:
        client.version.side_effect = raises
    else:
        client.version.return_value = version
    with patch("robovast.service.http_client.RobovastClient", return_value=client):
        return doc._service_version("https://svc.example")  # noqa: SLF001


def _client_checks(version=None, raises=None):
    """`check_client`'s build line, with the handshake stubbed."""
    return doc._check_build_capability(_handshake(version, raises))  # noqa: SLF001


def _revision_checks(version=None, raises=None, here="abc1234"):
    """`check_client`'s revision line, with the handshake and this side's revision stubbed."""
    with patch("robovast.client.app_version.running_revision", return_value=here):
        return doc._check_service_revision(_handshake(version, raises))  # noqa: SLF001


def test_a_service_that_gave_no_verdict_produces_no_line():
    """`None` is "did not say". A service older than the field must not be told to fix
    itself."""
    from robovast.service.interface import VersionInfo

    assert _client_checks(VersionInfo(robovast_version="2.0.0")) == []


def test_a_capable_service_reports_available():
    from robovast.service.interface import VersionInfo

    checks = _client_checks(VersionInfo(robovast_version="2.0.0", can_build_images=True))
    assert len(checks) == 1
    assert checks[0].ok is True


def test_an_incapable_service_carries_its_reason():
    from robovast.service.interface import VersionInfo

    checks = _client_checks(VersionInfo(
        robovast_version="2.0.0", can_build_images=False,
        build_unavailable="nowhere to push it. … 'vast exec cluster upgrade' …"))
    assert checks[0].ok is False
    assert "upgrade" in checks[0].fix
    assert checks[0].optional, "a service without a registry is not a broken install"


def test_an_unreadable_handshake_is_silent_rather_than_red():
    """A local `vast serve` whose token differs from the stored login answers 401. Turning
    that into a red line reports doctor's own credential mismatch as the service's fault."""
    assert _client_checks(raises=RuntimeError("401 Unauthorized")) == []


# -- which code is the service running --------------------------------------
#
# The failure: an edit made, a service that never reloaded it, and every symptom reading as
# a bug in the change. `vast --version` answers it for this side; this is the other side.


def _version(**kwargs):
    from robovast.service.interface import VersionInfo

    return VersionInfo(robovast_version="2.0.0", **kwargs)


def test_a_matching_revision_is_green_and_says_so():
    checks = _revision_checks(_version(code_revision="abc1234"), here="abc1234")
    assert len(checks) == 1
    assert checks[0].ok is True
    assert "abc1234" in checks[0].detail


def test_a_differing_revision_warns_and_names_the_roll():
    """Advisory, not fatal: being pointed at a deployment other than your own tree is
    normal, and a doctor that exited non-zero for it would be crying wolf."""
    checks = _revision_checks(_version(code_revision="abc1234"), here="def5678")
    assert checks[0].ok is False
    assert checks[0].optional
    assert "abc1234" in checks[0].detail and "def5678" in checks[0].detail
    assert "upgrade" in checks[0].fix


def test_no_reported_revision_is_distinguishable_from_a_mismatch():
    """The state every agent hit before the images baked their revision. "Cannot tell" must
    not be reported as "different code", which sends someone re-releasing a current
    service."""
    checks = _revision_checks(_version(code_revision=""), here="abc1234")
    assert checks[0].ok is False
    assert "not reported" in checks[0].detail
    assert "def5678" not in checks[0].detail
    assert "release-images" in checks[0].fix


def test_nothing_to_compare_against_is_not_a_mismatch():
    """A client-only or non-git install has no revision of its own. Reporting the service's
    and stopping beats inventing a comparison."""
    checks = _revision_checks(_version(code_revision="abc1234"), here="")
    assert checks[0].ok is True
    assert "abc1234" in checks[0].detail


def test_two_dirty_trees_are_not_claimed_to_match():
    """`+dirty` records that a tree was unclean; it cannot tell two unclean trees apart, so
    equality here is not proof and must not read as it."""
    checks = _revision_checks(_version(code_revision="abc1234+dirty"), here="abc1234+dirty")
    assert checks[0].ok is True
    assert "dirty" in checks[0].detail.lower()


def test_neither_side_having_one_says_nothing():
    """A client-only install talking to an older service: the remedy for "cannot report" is
    a re-release, which is not this user's job, and the service may be current anyway."""
    assert _revision_checks(_version(code_revision=""), here="") == []


def test_an_unreadable_handshake_says_nothing_about_the_revision():
    """Same rule as the build line: an unreachable or unauthorised service is the `service`
    check's business, not two more red lines."""
    assert _revision_checks(raises=RuntimeError("401 Unauthorized")) == []
