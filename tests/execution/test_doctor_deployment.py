# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``vast doctor`` says whether a deployment can build, and which remedy applies.

Without it, a deployment that cannot build is found out by a campaign refused at submit,
after a project push, a workspace create and a launch. And the remedy differs: ``setup
--ingress-host`` for a service *never published*, ``upgrade`` for one that was published and
lost its prefix. The in-pod service cannot tell those apart — it reads the prefix out of its
environment and has no RBAC to read its own Ingress. From a machine with a kubeconfig they
*are* distinguishable, which is why this check lives in the cluster package.

Two names on purpose: ``build registry`` and ``registry route`` describe the
infrastructure, while the client-side ``image builds`` reports what the running service
says it can do. They can legitimately disagree — a pod predating its own config — and two
rows sharing one name would read as a single check contradicting itself.
"""

# pylint: disable=redefined-outer-name  # the pytest fixture idiom
# pylint: disable=protected-access  # _check_job_placement is what this file tests

import types
from unittest.mock import MagicMock

import pytest

from robovast.client.doctor import Check, DoctorOptions
from robovast.execution.cluster_execution import doctor as doc


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
    # The build-daemon read, which lands on whichever cluster the kubeconfig points at, and
    # waits for it to time out, if it is not stubbed.
    from robovast.execution.cluster_execution import buildkitd_deploy, kube_client
    monkeypatch.setattr(kube_client, "load_kube_config", lambda ctx=None: None)
    monkeypatch.setattr(buildkitd_deploy, "buildkitd_ready", lambda ns: state.daemon_ready)
    # The job placement rows read the live Deployment and the node list; they have tests of
    # their own below and are not what this fixture's tests are about.
    monkeypatch.setattr(doc, "_check_job_placement", lambda ns, ctx: [])
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
    assert "vast service upgrade" in check.fix
    assert "setup" not in check.fix, (
        "with the Ingress readable, this check knows which remedy applies -- offering "
        "both here would put the ambiguity back that it exists to resolve")


def test_not_published_names_setup_with_its_tls_options(deployment):
    deployment.config, deployment.prefix, deployment.host = "rke2", "", ""

    check = _by_name(doc.check_deployment(), "build registry")
    assert check.ok is False
    assert "vast cluster setup" in check.fix
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
    assert "vast service upgrade" in route.fix

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
    """The gate in `doctor_checks`. Asking a deployment about itself over a dead API server
    is a second way of saying "no cluster"."""
    monkeypatch.setattr(doc, "check_tools", lambda flavor="": [])
    monkeypatch.setattr(doc, "check_cluster",
                        lambda context=None: [Check("kubeconfig", False, "none")])

    def _must_not_run(*_a, **_k):
        raise AssertionError("check_deployment ran despite an unusable cluster")

    monkeypatch.setattr(doc, "check_deployment", _must_not_run)
    assert [c.name for c in doc.doctor_checks(DoctorOptions())] == ["kubeconfig"]


def test_the_options_reach_the_deployment_checks(monkeypatch):
    """`-x` and `-n` are what `vast doctor` was asked about; the plugin must use both."""
    seen = {}
    monkeypatch.setattr(doc, "check_tools", lambda flavor="": seen.update(flavor=flavor) or [])
    monkeypatch.setattr(doc, "check_cluster", lambda context=None: (
        seen.update(cluster_context=context) or [Check("kubeconfig", True, "cfg")]))
    monkeypatch.setattr(doc, "check_deployment", lambda namespace, context: (
        seen.update(namespace=namespace, context=context) or []))

    doc.doctor_checks(DoctorOptions(flavor="gcp", context="ctx", namespace="ns"))
    assert seen == {"flavor": "gcp", "cluster_context": "ctx", "namespace": "ns",
                    "context": "ctx"}


# -- job placement ----------------------------------------------------------------------

def _placement(monkeypatch, nodes, pool):
    from robovast.execution.cluster_execution import kube_client, service_deploy
    from tests.execution.test_job_node_alias import _Core

    monkeypatch.setattr(kube_client, "load_kube_config", lambda ctx=None: None)
    if isinstance(pool, Exception):
        def _raise(ns, ctx):
            raise pool
        monkeypatch.setattr(service_deploy, "job_node_pool_from_cluster", _raise)
    else:
        monkeypatch.setattr(service_deploy, "job_node_pool_from_cluster",
                            lambda ns, ctx: pool)
    monkeypatch.setattr("kubernetes.client.CoreV1Api", lambda: _Core(*nodes))
    return doc._check_job_placement("default", None)


def test_a_pool_matching_no_node_fails(monkeypatch):
    from tests.execution.test_job_node_alias import _Node
    checks = _placement(monkeypatch, [_Node("node-a")], {"node-pool": "typo"})
    row = _by_name(checks, "job node pool")
    assert not row.ok and not row.optional
    assert "matches no node" in row.detail


def test_a_pool_of_unschedulable_nodes_fails(monkeypatch):
    from tests.execution.test_job_node_alias import POOL, _pooled
    row = _by_name(_placement(monkeypatch, [_pooled("node-a", cordoned=True)], POOL),
                   "job node pool")
    assert not row.ok and "none schedulable" in row.detail


def test_a_usable_pool_is_green(monkeypatch):
    from tests.execution.test_job_node_alias import POOL, _pooled
    row = _by_name(_placement(monkeypatch, [_pooled("node-a")], POOL), "job node pool")
    assert row.ok


def test_an_unparseable_pool_fails(monkeypatch):
    checks = _placement(monkeypatch, [], ValueError("ROBOVAST_JOB_NODE_LABELS='x' is not JSON"))
    assert not _by_name(checks, "job node pool").ok


@pytest.mark.parametrize("nodes,cause", [
    pytest.param(lambda m: [m._Node("node-a", {m.ALIAS: "bench", m.np.NODE_ID_LABEL: "i"}),
                            m._pooled("node-b")], "outside-pool", id="outside-pool"),
    pytest.param(lambda m: [m._pooled("node-a", labels={m.ALIAS: "bench"}),
                            m._pooled("node-b", labels={m.ALIAS: "bench"})],
                 "ambiguous", id="on-two-nodes"),
    pytest.param(lambda m: [m._pooled("node-a", labels={m.ALIAS: "bench"}, cordoned=True),
                            m._pooled("node-b")], "unschedulable", id="cordoned"),
])
def test_a_dangling_alias_fails_naming_the_alias(monkeypatch, nodes, cause):
    from tests.execution import test_job_node_alias as m
    row = _by_name(_placement(monkeypatch, nodes(m), m.POOL), "job node alias bench")
    assert not row.ok and not row.optional
    assert cause in row.detail


def test_a_resolvable_alias_is_green(monkeypatch):
    from tests.execution import test_job_node_alias as m
    checks = _placement(monkeypatch, [m._pooled("node-a", labels={m.ALIAS: "bench"})], m.POOL)
    assert _by_name(checks, "job node alias bench").ok


def test_placement_is_checked_on_an_unpublished_deployment(deployment, monkeypatch):
    """Unrelated to the registry: a pool that matches nothing stops every campaign."""
    monkeypatch.setattr(doc, "_check_job_placement",
                        lambda ns, ctx: [Check("job node pool", False, "x")])
    names = [c.name for c in doc.check_deployment()]
    assert "build registry" in names and "job node pool" in names
