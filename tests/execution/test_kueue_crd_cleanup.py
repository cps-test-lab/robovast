# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A teardown that leaves Kueue's CRDs behind is not a teardown.

``helm uninstall`` never deletes CRDs — a chart's ``crds/`` directory is install-only,
because deleting a CRD destroys every object of that kind. Harmless for an upgrade;
for a *cleanup* it leaves the CRDs un-owned, and the next ``helm upgrade --install``
refuses with "invalid ownership metadata ... missing key meta.helm.sh/release-name".

The cluster is then permanently un-setup-able from RoboVAST, and the error names neither
the cause nor the remedy. Observed on a real cluster: one leftover
``clusterqueues.kueue.x-k8s.io``, carrying Helm's labels and none of its annotations,
blocking every subsequent setup.
"""

from types import SimpleNamespace
from unittest import mock

import pytest

from robovast.execution.cluster_execution import kubernetes_kueue as kq


def _crd(name, group="kueue.x-k8s.io", annotations=None):
    return SimpleNamespace(
        spec=SimpleNamespace(group=group),
        metadata=SimpleNamespace(name=name, annotations=annotations))


@pytest.fixture(autouse=True)
def _no_kube(monkeypatch):
    monkeypatch.setattr("robovast.common.kube.load_kube_config", lambda context=None: "")


def test_a_crd_without_helm_ownership_is_reported_as_orphaned(monkeypatch):
    api = mock.Mock()
    api.list_custom_resource_definition.return_value = SimpleNamespace(items=[
        _crd("clusterqueues.kueue.x-k8s.io",
             annotations={"controller-gen.kubebuilder.io/version": "v0.20.0"}),
    ])
    monkeypatch.setattr("kubernetes.client.ApiextensionsV1Api", lambda: api)

    orphans = kq.orphaned_kueue_crds()
    assert [name for name, _ in orphans] == ["clusterqueues.kueue.x-k8s.io"]


def test_an_owned_crd_is_left_alone(monkeypatch):
    api = mock.Mock()
    api.list_custom_resource_definition.return_value = SimpleNamespace(items=[
        _crd("clusterqueues.kueue.x-k8s.io",
             annotations={"meta.helm.sh/release-name": "kueue"}),
    ])
    monkeypatch.setattr("kubernetes.client.ApiextensionsV1Api", lambda: api)
    assert kq.orphaned_kueue_crds() == []


def test_another_projects_crds_are_never_touched(monkeypatch):
    """Only this group. Deleting somebody else's CRD destroys their objects."""
    api = mock.Mock()
    api.list_custom_resource_definition.return_value = SimpleNamespace(items=[
        _crd("rayclusters.ray.io", group="ray.io", annotations={}),
    ])
    monkeypatch.setattr("kubernetes.client.ApiextensionsV1Api", lambda: api)
    assert kq.orphaned_kueue_crds() == []

    kq.delete_kueue_crds(timeout_s=1)
    api.delete_custom_resource_definition.assert_not_called()


def test_adoption_stamps_ownership_rather_than_deleting(monkeypatch):
    """Deleting would destroy live ClusterQueues for a bookkeeping problem."""
    api = mock.Mock()
    api.list_custom_resource_definition.return_value = SimpleNamespace(items=[
        _crd("clusterqueues.kueue.x-k8s.io", annotations={}),
    ])
    monkeypatch.setattr("kubernetes.client.ApiextensionsV1Api", lambda: api)

    kq.adopt_orphaned_kueue_crds()

    api.delete_custom_resource_definition.assert_not_called()
    name, patch = api.patch_custom_resource_definition.call_args[0]
    assert name == "clusterqueues.kueue.x-k8s.io"
    annotations = patch["metadata"]["annotations"]
    assert annotations["meta.helm.sh/release-name"] == kq.KUEUE_HELM_RELEASE
    assert annotations["meta.helm.sh/release-namespace"] == kq.KUEUE_NAMESPACE


def test_cleanup_deletes_the_crds_helm_leaves(monkeypatch):
    api = mock.Mock()
    present = [_crd("clusterqueues.kueue.x-k8s.io"), _crd("workloads.kueue.x-k8s.io")]

    def _list():
        return SimpleNamespace(items=present)

    api.list_custom_resource_definition.side_effect = _list

    def _delete(name):
        present[:] = [c for c in present if c.metadata.name != name]

    api.delete_custom_resource_definition.side_effect = _delete
    monkeypatch.setattr("kubernetes.client.ApiextensionsV1Api", lambda: api)

    kq.delete_kueue_crds(timeout_s=10)
    assert present == []


def test_a_crd_that_will_not_delete_fails_loudly(monkeypatch):
    """Silence here means the next setup breaks instead, far from the cause."""
    api = mock.Mock()
    api.list_custom_resource_definition.return_value = SimpleNamespace(
        items=[_crd("clusterqueues.kueue.x-k8s.io")])
    monkeypatch.setattr("kubernetes.client.ApiextensionsV1Api", lambda: api)
    monkeypatch.setattr(kq, "_clear_finalizers_on_kueue_objects", lambda names: None)

    with pytest.raises(RuntimeError) as excinfo:
        kq.delete_kueue_crds(timeout_s=1)

    message = str(excinfo.value)
    assert "clusterqueues.kueue.x-k8s.io" in message
    # It must name the consequence and the manual remedy, not just the failure.
    assert "invalid ownership metadata" in message
    assert "kubectl delete crd" in message


def test_uninstall_removes_the_crds_too(monkeypatch):
    """The regression: cleanup stopped at `helm uninstall` and called it done."""
    monkeypatch.setattr(kq, "cleanup_kueue_cluster_resources", lambda **k: None)
    monkeypatch.setattr(kq, "_run_helm", lambda *a, **k: (True, ""))
    called = []
    monkeypatch.setattr(kq, "delete_kueue_crds",
                        lambda **k: called.append("delete_kueue_crds"))

    kq.uninstall_kueue_helm()
    assert called == ["delete_kueue_crds"]


def test_install_adopts_before_helm_runs(monkeypatch):
    """Order matters: helm refuses the install before we would get a chance to fix it."""
    order = []
    monkeypatch.setattr(kq, "adopt_orphaned_kueue_crds",
                        lambda **k: order.append("adopt"))
    monkeypatch.setattr(kq.subprocess, "run",
                        lambda *a, **k: order.append("helm") or SimpleNamespace(
                            returncode=1, stdout="", stderr=""))
    monkeypatch.setattr(kq, "_run_helm", lambda *a, **k: (True, ""))
    monkeypatch.setattr(kq, "_wait_for_kueue_ready", lambda **k: None, raising=False)

    try:
        kq.install_kueue_helm()
    except Exception:  # noqa: BLE001 - the install path beyond helm is not under test
        pass

    assert order[:2] == ["adopt", "helm"], order
