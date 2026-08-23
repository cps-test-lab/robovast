# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""An upgrade must not move the service pod, nor the volumes under it.

The bug: `vast exec cluster upgrade` called `deploy_service` with neither the node pin nor
the storage classes, and "not passed" meant "unpinned, on a hostPath". One upgrade therefore
unpinned the service pod and reverted a PVC-backed registry -- silently, on the deployment
whose data was the reason those flags were given in the first place.

Two changes make that unreachable, and these tests pin both: the selector is a CONSTANT
label rather than a value a call site has to remember, and everything else is recovered from
the live Deployment instead of defaulted.
"""

import json

import pytest

from robovast.execution.cluster_execution import node_placement as np
from robovast.execution.cluster_execution import service_deploy as sd


def _deserialize(manifest, kind):
    """The manifest as the API would hand it back: a typed object, not the dict."""
    from kubernetes.client import ApiClient

    class _Response:
        def __init__(self, data):
            self.data = json.dumps(data)

    return ApiClient().deserialize(_Response(manifest), kind)


def _api_error(status):
    from kubernetes.client.exceptions import ApiException
    return ApiException(status=status, reason="canned")


def _cluster(monkeypatch, *, dep=None, dep_error=None, pvc=None):
    """Point `service_storage_from_cluster` at a canned cluster."""
    from kubernetes import client as kclient

    from robovast.execution.cluster_execution import kube_client

    class _Apps:
        def read_namespaced_deployment(self, name, namespace):
            if dep_error is not None:
                raise dep_error
            return dep

    class _Core:
        def read_namespaced_persistent_volume_claim(self, name, namespace):
            return pvc

    monkeypatch.setattr(kube_client, "load_kube_config", lambda *a, **k: None)
    monkeypatch.setattr(kclient, "AppsV1Api", lambda *a, **k: _Apps())
    monkeypatch.setattr(kclient, "CoreV1Api", lambda *a, **k: _Core())


def _live(**kwargs):
    """A deployed service Deployment, as the API would return it."""
    return _deserialize(sd._deployment_manifest("ns", "img:latest", **kwargs),
                        "V1Deployment")


def _pvc(storage_class):
    return _deserialize(
        {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
         "metadata": {"name": "registry-data", "namespace": "ns"},
         "spec": {"accessModes": ["ReadWriteOnce"], "storageClassName": storage_class,
                  "resources": {"requests": {"storage": "50Gi"}}}},
        "V1PersistentVolumeClaim")


# --- the pin ----------------------------------------------------------------------

def test_the_pin_is_a_constant_a_caller_cannot_forget():
    """The structural half of the fix.

    There is no node *value* to thread through `deploy_service`, so the call site that used
    to drop it has nothing left to drop. Rendering the pin twice must produce the same
    selector without either render being told which node it is.
    """
    def _rendered():
        manifest = sd._deployment_manifest(
            "ns", "img:latest", node_selector=np.label_selector(np.DATA_NODE_LABEL))
        return manifest["spec"]["template"]["spec"]["nodeSelector"]

    first, second = _rendered(), _rendered()
    assert first == second == {np.DATA_NODE_LABEL: "true"}
    assert not any(k == "kubernetes.io/hostname" for k in first)


def test_an_upgrade_keeps_the_pin_it_found(monkeypatch):
    """`deploy_service` is called with no selector at all -- the upgrade path."""
    selector = np.label_selector(np.DATA_NODE_LABEL)
    _cluster(monkeypatch, dep=_live(node_selector=selector))
    monkeypatch.setattr(np, "resolve_placement",
                        lambda core, label, **kw: np.Placement("node-a",
                                                               np.label_selector(label),
                                                               "label"))
    recovered = sd._resolve_data_node(None, deployed_selector=selector)
    assert recovered == {np.DATA_NODE_LABEL: "true"}


def test_an_upgrade_with_no_label_left_keeps_the_live_pods_selector(monkeypatch):
    """An operator who removed the label by hand must not thereby get a floating pod.

    `resolve_placement` returns None here (an upgrade may not pick a node), so the only
    safe answer is whatever the running pod already has -- never "unpinned".
    """
    monkeypatch.setattr(np, "resolve_placement", lambda core, label, **kw: None)
    deployed = {np.DATA_NODE_LABEL: "true"}
    assert sd._resolve_data_node(None, deployed_selector=deployed) == deployed


def test_a_provisioned_volume_is_not_pinned(monkeypatch):
    """Both volumes on a StorageClass: nothing is on a node, so nothing is pinned."""
    assert sd._resolve_data_node(None, registry_storage_class="fast",
                                 workspaces_storage_class="fast") == {}


def test_one_hostpath_volume_is_enough_to_keep_the_pod_pinned(monkeypatch):
    """The pod carries both volumes, so a provisioned registry does not free it to move
    while the workspaces are still a hostPath under it."""
    monkeypatch.setattr(np, "resolve_placement",
                        lambda core, label, **kw: np.Placement("node-a",
                                                               np.label_selector(label),
                                                               "label"))
    assert sd._resolve_data_node(None, registry_storage_class="fast") == {
        np.DATA_NODE_LABEL: "true"}


# --- the volumes ------------------------------------------------------------------

@pytest.mark.parametrize("rendered,recovered", [
    ({"registry_storage_class": "fast"}, {"registry_storage_class": "fast"}),
    ({"registry_storage_path": "/data/elsewhere"},
     {"registry_storage_path": "/data/elsewhere"}),
    ({"workspaces_storage_path": "/srv/work"}, {"workspaces_storage_path": "/srv/work"}),
])
def test_an_upgrade_re_renders_the_volumes_it_found(monkeypatch, rendered, recovered):
    """Recovered, never defaulted: re-rendering from defaults handed a PVC-backed registry
    a hostPath -- a new empty registry, while the old claim still held its space."""
    _cluster(monkeypatch, dep=_live(**rendered),
             pvc=_pvc(rendered.get("registry_storage_class", "")))
    settings = sd.service_storage_from_cluster("ns")
    for key, value in recovered.items():
        assert settings[key] == value


def test_a_claim_with_no_storage_class_is_refused_not_defaulted(monkeypatch):
    """`registry_volume` takes the PVC branch only for a non-empty class, so recovering an
    empty one silently becomes a hostPath -- the exact migration this reader prevents."""
    _cluster(monkeypatch, dep=_live(registry_storage_class="fast"), pvc=_pvc(None))
    with pytest.raises(RuntimeError, match="StorageClass"):
        sd.service_storage_from_cluster("ns")


def test_no_deployment_yet_is_an_answer_not_an_error(monkeypatch):
    """A first setup has nothing to recover; the caller uses its own defaults."""
    _cluster(monkeypatch, dep_error=_api_error(404))
    assert sd.service_storage_from_cluster("ns") == {}


def test_an_unreachable_cluster_is_not_mistaken_for_no_deployment(monkeypatch):
    """Defaulting on a failed read is precisely the silent migration above."""
    _cluster(monkeypatch, dep_error=_api_error(500))
    with pytest.raises(Exception):
        sd.service_storage_from_cluster("ns")
