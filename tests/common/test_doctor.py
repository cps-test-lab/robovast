# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``vast doctor`` — each check exists because its absence used to surface badly.

The properties worth pinning are about the *message*, not the verdict. A check that
reports "helm: missing" and stops has moved the problem rather than solved it, so every
failure here has to carry its remedy. And an optional dependency must not fail the run,
or the cluster path would demand Docker it never uses.
"""

from types import SimpleNamespace
from unittest import mock

import pytest

from robovast.client import doctor


def test_a_current_python_passes():
    check = doctor.check_python()
    assert check.ok and check.status == "ok"


def test_an_old_python_says_how_to_fix_it(monkeypatch):
    monkeypatch.setattr(doctor.sys, "version_info", (3, 10, 0, "final", 0))
    check = doctor.check_python()
    assert not check.ok
    assert "make venv" in check.fix


def test_a_missing_tool_names_where_to_get_it(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    checks = {c.name: c for c in doctor.check_tools()}
    assert not checks["helm"].ok
    assert "helm.sh" in checks["helm"].fix
    assert not checks["kubectl"].ok
    assert "kubernetes.io" in checks["kubectl"].fix


def test_docker_is_optional_so_the_cluster_path_does_not_demand_it(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which",
                        lambda name: None if name == "docker" else f"/usr/bin/{name}")
    docker = next(c for c in doctor.check_tools() if c.name == "docker")
    assert not docker.ok
    assert docker.optional
    assert docker.status == "warn"


def test_gcloud_is_only_checked_for_the_gcp_flavor(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert "gcloud" not in {c.name for c in doctor.check_tools()}
    assert "gcloud" in {c.name for c in doctor.check_tools(flavor="gcp")}


def test_an_unusable_kubeconfig_is_a_single_actionable_failure(monkeypatch):
    monkeypatch.setattr("robovast.execution.cluster_execution.kube_client.load_kube_config",
                        mock.Mock(side_effect=RuntimeError("no kubeconfig")))
    checks = doctor.check_cluster()
    assert [c.name for c in checks] == ["kubeconfig"]
    assert "use-context" in checks[0].fix


def _node(cpu, memory):
    return SimpleNamespace(status=SimpleNamespace(
        allocatable={"cpu": cpu, "memory": memory}))


@pytest.mark.parametrize("cpu,memory,ok", [
    pytest.param("8", "32Gi", True, id="comfortable"),
    pytest.param("2", "4Gi", True, id="small-but-usable"),
    pytest.param("8000m", "32Gi", True, id="millicores"),
    pytest.param("0", "0", False, id="schedules-nothing"),
])
def test_capacity_fails_only_on_a_cluster_that_can_run_nothing(monkeypatch, cpu, memory, ok):
    """No fixed threshold, deliberately.

    This asserted 4 CPU / 16 GiB when Kueue's controller had to fit on one node. Nothing
    is deployed with a fixed size now, and a campaign pod is whatever its ``.vast`` asks
    for -- so a number here would be invented, and admission already refuses an oversized
    request at launch while naming both the request and each node's allocatable. A small
    cluster is small, not broken; one advertising nothing is broken.
    """
    core = mock.Mock()
    core.list_node.return_value = SimpleNamespace(items=[_node(cpu, memory)])
    monkeypatch.setattr("kubernetes.client.CoreV1Api", lambda: core)

    check = doctor._check_capacity()
    assert check.ok is ok
    if not ok:
        assert "nothing can be scheduled" in check.fix


def test_capacity_reports_the_largest_node_not_the_total(monkeypatch):
    """A pod runs on one node, so the largest node is the number that decides what fits --
    and it is what an operator needs when admission refuses a request as too large. Summing
    would describe a cluster that can take a big pod when no single node can."""
    core = mock.Mock()
    core.list_node.return_value = SimpleNamespace(
        items=[_node("2", "8Gi"), _node("4", "8Gi"), _node("2", "8Gi")])
    monkeypatch.setattr("kubernetes.client.CoreV1Api", lambda: core)

    assert "largest node: 4.0 CPU" in doctor._check_capacity().detail


def test_a_namespaced_kubeconfig_is_reported_before_setup_dies_on_it(monkeypatch):
    api = mock.Mock()
    api.create_self_subject_access_review.return_value = SimpleNamespace(
        status=SimpleNamespace(allowed=False))
    monkeypatch.setattr("kubernetes.client.AuthorizationV1Api", lambda: api)

    check = doctor._check_rbac()
    assert not check.ok
    assert "cluster-admin" in check.fix


def test_sufficient_permissions_pass(monkeypatch):
    api = mock.Mock()
    api.create_self_subject_access_review.return_value = SimpleNamespace(
        status=SimpleNamespace(allowed=True))
    monkeypatch.setattr("kubernetes.client.AuthorizationV1Api", lambda: api)
    assert doctor._check_rbac().ok


def test_every_failure_carries_a_remedy(monkeypatch):
    """The rule the whole command rests on."""
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    monkeypatch.setattr("robovast.execution.cluster_execution.kube_client.load_kube_config",
                        mock.Mock(side_effect=RuntimeError("nope")))
    for check in doctor.run_checks(flavor="gcp"):
        if not check.ok:
            assert check.fix, f"{check.name} failed without saying what to do"
