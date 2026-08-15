# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The registry RoboVAST runs for itself, beside the service.

The load-bearing constraint is that an image ref is ONE string used by two different
resolvers: BuildKit pushes to it from a pod, and the kubelet pulls it on the node. The
node reads neither CoreDNS nor the pod spec, so anything cluster-internal (`.svc`, a
hostAlias) works for the push and fails for the pull. Publishing on the service's own
Ingress host is what makes one string satisfy both, and most of what these tests pin is
that the pieces of that arrangement stay consistent.
"""

from robovast.execution.cluster_execution import registry_deploy as rd
from robovast.execution.cluster_execution import service_deploy as sd


def _pod(**kwargs):
    manifest = sd._deployment_manifest("default", "img:latest", **kwargs)
    return manifest["spec"]["template"]["spec"]


def _registry_container(pod):
    return next(c for c in pod["containers"] if c["name"] == rd.REGISTRY_CONTAINER_NAME)


def test_the_registry_runs_beside_the_service_in_one_pod():
    """One pod, so one restart covers both and one Service fronts both."""
    pod = _pod()
    names = [c["name"] for c in pod["containers"]]
    assert names == [sd.SERVICE_NAME, rd.REGISTRY_CONTAINER_NAME]


def test_the_registry_has_somewhere_durable_to_keep_blobs():
    """Never emptyDir.

    Every upgrade restarts this pod now, so an emptyDir registry would drop every built
    image on each version bump -- and campaign Jobs already submitted against those refs
    would hit ImagePullBackOff rather than fail with a reason.
    """
    volume = next(v for v in _pod()["volumes"] if v["name"] == rd.REGISTRY_VOLUME_NAME)
    assert "emptyDir" not in volume
    assert volume["hostPath"]["path"] == rd.DEFAULT_REGISTRY_HOST_PATH
    mount = _registry_container(_pod())["volumeMounts"][0]
    assert mount["mountPath"] == rd.REGISTRY_DATA_DIR


def test_a_storage_class_switches_the_volume_to_a_pvc():
    pod = _pod(registry_storage_class="local-path")
    volume = next(v for v in pod["volumes"] if v["name"] == rd.REGISTRY_VOLUME_NAME)
    assert volume["persistentVolumeClaim"]["claimName"] == rd.REGISTRY_VOLUME_NAME
    assert "hostPath" not in volume

    pvc = rd.registry_pvc_manifest("default", "local-path")
    assert pvc["spec"]["storageClassName"] == "local-path"
    assert rd.registry_pvc_manifest("default", "") is None


def test_hostpath_storage_can_be_pinned_to_its_node():
    """hostPath puts the blobs on one node's disk; rescheduling elsewhere would come up
    with an empty registry and every previously built image would silently be gone."""
    assert "nodeSelector" not in _pod()
    pod = _pod(registry_node="node-02")
    assert pod["nodeSelector"] == {"kubernetes.io/hostname": "node-02"}


def test_the_registry_is_reachable_through_the_service():
    """The Ingress routes to a Service port, so the port has to be published there."""
    service = sd._service_manifest("default")
    ports = {p["name"]: p["port"] for p in service["spec"]["ports"]}
    assert ports["registry"] == rd.REGISTRY_PORT
    assert ports["http"] == sd.SERVICE_PORT


def test_deletes_are_enabled_so_a_rebuilt_image_can_be_reclaimed():
    env = {e["name"]: e["value"] for e in _registry_container(_pod())["env"]}
    assert env["REGISTRY_STORAGE_DELETE_ENABLED"] == "true"


def test_the_registry_reports_readiness_on_the_registry_api():
    container = _registry_container(_pod())
    assert container["readinessProbe"]["httpGet"]["path"] == "/v2/"
    assert container["readinessProbe"]["httpGet"]["port"] == rd.REGISTRY_PORT


def test_the_ingress_path_and_the_prefix_describe_the_same_registry():
    """A registry answers at ``/v2`` of its host, which is why the prefix is a bare host
    with no path -- if these two ever disagreed, pushes and pulls would address
    different places."""
    assert rd.REGISTRY_INGRESS_PATH == "/v2"
    assert rd.registry_prefix("h.example.org") == "h.example.org"
    assert rd.registry_ingress_path()["backend"]["service"]["port"]["number"] == \
        rd.REGISTRY_PORT
