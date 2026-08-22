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

    state = types.SimpleNamespace(config="rke2", prefix="", host="", defects=[],
                                 daemon_ready=True)

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
    # The build-daemon read, which lands on a real cluster if it is not stubbed. It was
    # added after this fixture and went unstubbed, so the two tests that reach it (the ones
    # with a registry prefix -- the only path that asks) each spent 40 seconds waiting for
    # the configured context to time out before `_check_build_daemon` swallowed the error.
    # Eighty of this file's eighty-one seconds, and a result that silently depended on
    # whichever cluster the developer's kubeconfig happened to point at.
    from robovast.execution.cluster_execution import buildkitd_deploy, kube_client
    monkeypatch.setattr(kube_client, "load_kube_config", lambda ctx=None: None)
    monkeypatch.setattr(buildkitd_deploy, "buildkitd_ready", lambda ns: state.daemon_ready)
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


def test_a_missing_build_daemon_is_reported_beside_the_registry(deployment):
    """Nothing can build without it, and a campaign should not be how you find out.

    Asked only where it can be answered usefully -- next to a configured registry, since
    "the daemon is down" is noise on a deployment that has nowhere to push anyway.
    """
    deployment.prefix, deployment.daemon_ready = "registry.example.org", False

    check = _by_name(doc.check_deployment(), "build daemon")
    assert check.ok is False and check.optional
    assert "no ready pod" in check.detail


def test_a_ready_build_daemon_is_green(deployment):
    deployment.prefix, deployment.daemon_ready = "registry.example.org", True

    assert _by_name(doc.check_deployment(), "build daemon").ok is True


def test_the_build_daemon_is_not_asked_about_without_a_registry(deployment):
    """No push target means no build question -- one fault, one line."""
    deployment.prefix = ""

    names = [c.name for c in doc.check_deployment()]
    assert "build daemon" not in names


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
    """`check_client`'s build line, with the handshake stubbed.

    Splatted, because the handshake answers ``(info, error)``: the error is returned rather
    than swallowed so that a service which ANSWERED and refused can be told apart from one
    nothing replied to. Passing the pair as ``info`` alone is not a type error -- a tuple has
    no ``code_revision``, so every case reads as "the service did not report one" and the
    branch under test is never reached.
    """
    return doc._check_build_capability(*_handshake(version, raises))  # noqa: SLF001


def _revision_checks(version=None, raises=None, here="abc1234"):
    """`check_client`'s revision line, with the handshake and this side's revision stubbed."""
    with patch("robovast.client.app_version.running_revision", return_value=here):
        return doc._check_service_revision(*_handshake(version, raises))  # noqa: SLF001


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


def test_an_unreachable_service_says_nothing_about_the_revision():
    """Silent only when nothing replied: that fault belongs to the `service` row alone.

    A bare exception with no HTTP status is a request that never got an answer -- DNS, TCP,
    TLS, a timeout, a pod mid-roll. The `service` row is red and names it, and a second red
    line for the same fault sends a reader chasing it twice.
    """
    assert _revision_checks(raises=RuntimeError("connection refused")) == []


def test_a_refused_handshake_says_which_credential_fixes_it():
    """Answered-and-refused is a DIFFERENT fault, and it used to vanish.

    A service whose token does not match this deployment answers 401. That is not "no
    revision question exists", it is "your credentials cannot ask it" -- and while this row
    stayed silent for it, the reader saw nothing to report at all.
    """
    checks = _revision_checks(raises=_Refused(401, "token does not match"))

    assert len(checks) == 1
    assert checks[0].ok is False and checks[0].optional
    assert "401" in checks[0].detail
    assert "vast login" in checks[0].fix


def test_a_refused_handshake_also_speaks_on_the_build_line():
    """Same rule, same reason, on the row that answers "can this service build?"."""
    checks = _client_checks(raises=_Refused(403, "forbidden"))

    assert len(checks) == 1
    assert checks[0].ok is False


def _service_row(target, err, monkeypatch):
    """``check_client``'s `service` row, with the handshake and the PATH probe stubbed."""
    from robovast.client import login as login_config
    from robovast.client import service_target

    monkeypatch.setattr(login_config, "credentials",
                        lambda: (target, "tok", "me") if target else ("", "", ""))
    monkeypatch.setattr(service_target, "detected_service_url", lambda: target)
    monkeypatch.setattr(doc, "_service_version", lambda t: (None, err))
    monkeypatch.setattr(doc, "_check_service_revision", lambda *a: [])
    monkeypatch.setattr(doc, "_check_build_capability", lambda *a: [])
    monkeypatch.setattr(doc.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(returncode=1, stdout=""))
    return next(c for c in doc.check_client() if c.name == "service")


def test_the_service_row_is_green_only_when_the_service_answered(monkeypatch):
    """A stored login is configuration, not reachability.

    This row was green whenever a URL was configured, while the revision and build rows
    silently vanished because the handshake could not be read. Observed against a cluster
    mid-roll: a green service line, two missing lines, and every call timing out. The row's
    own failure wording is "none answering", so it already promised what it did not check.
    """
    row = _service_row("https://svc.example", None, monkeypatch)
    assert row.ok is True


def test_a_configured_but_silent_service_is_red_and_says_where_to_look(monkeypatch):
    row = _service_row("https://svc.example", RuntimeError("timed out"), monkeypatch)

    assert row.ok is False
    assert "not answering" in row.detail
    assert "mid-roll" in row.fix or "upgrade" in row.fix


def test_a_service_that_answered_and_refused_is_not_the_service_rows_fault(monkeypatch):
    """401 means it was reached, parsed the request and refused it.

    Only a request that never got an answer belongs on this row; the refusal is reported by
    the rows that own it, which name the credential that fixes it. Reading a 401 as "not
    answering" would send someone restarting a service that is up and working.
    """
    row = _service_row("https://svc.example", _Refused(401, "bad token"), monkeypatch)
    assert row.ok is True


class _Refused(Exception):
    """A service that answered and refused. ``status`` is what makes "it answered" decidable."""

    def __init__(self, status, detail=""):
        super().__init__(detail or f"HTTP {status}")
        self.status = status
        self.detail = detail
