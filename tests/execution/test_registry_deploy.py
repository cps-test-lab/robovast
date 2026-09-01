# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The registry RoboVAST runs for itself, in the object-store pod.

The load-bearing constraint is that an image ref is ONE string used by two different
resolvers: BuildKit pushes to it from a pod, and the kubelet pulls it on the node. The
node reads neither CoreDNS nor the pod spec, so anything cluster-internal (`.svc`, a
hostAlias) works for the push and fails for the pull. Publishing on the service's own
Ingress host is what makes one string satisfy both, and most of what these tests pin is
that the pieces of that arrangement stay consistent.
"""

import io

import yaml

from robovast.execution.cluster_execution import registry_deploy as rd
from robovast.execution.cluster_execution import service_deploy as sd
from robovast.execution.cluster_execution import store_pod


def _store_docs(namespace="default", **kwargs):
    from robovast.execution.cluster_config.rke2 import MINIO_MANIFEST_RKE2

    return store_pod.attach_infrastructure(
        list(yaml.safe_load_all(io.StringIO(MINIO_MANIFEST_RKE2))), namespace, **kwargs)


def _pod(**kwargs):
    return next(d for d in _store_docs(**kwargs) if d["kind"] == "Pod")["spec"]


def _registry_container(pod):
    return next(c for c in pod["containers"] if c["name"] == rd.REGISTRY_CONTAINER_NAME)


def test_the_registry_runs_in_the_store_pod_not_the_service_pod():
    """Setup-lifetime infrastructure, not service-lifetime.

    Every ``vast service upgrade`` rolls the service Deployment, so a registry there
    restarted on each version bump and kept its blobs on a volume that followed the
    Deployment. The store pod is created once at setup and torn down only by cleanup.
    """
    service_pod = sd._deployment_manifest(
        "default", "img:latest")["spec"]["template"]["spec"]
    # Exhaustive on purpose: a container appearing here unnoticed is a pod nobody sized.
    assert [c["name"] for c in service_pod["containers"]] == [sd.SERVICE_NAME]

    assert [c["name"] for c in _pod()["containers"]] == [
        "minio", rd.REGISTRY_CONTAINER_NAME, "index"]


def test_the_registry_has_somewhere_durable_to_keep_blobs():
    """Never emptyDir.

    A crash, an eviction or a node reboot would otherwise drop every built image -- and
    campaign Jobs already submitted against those refs would hit ImagePullBackOff rather
    than fail with a reason.
    """
    volume = next(v for v in _pod()["volumes"] if v["name"] == rd.REGISTRY_VOLUME_NAME)
    assert "emptyDir" not in volume
    assert volume["hostPath"]["path"] == rd.DEFAULT_REGISTRY_HOST_PATH
    mount = _registry_container(_pod())["volumeMounts"][0]
    assert mount["mountPath"] == rd.REGISTRY_DATA_DIR


def test_a_storage_class_switches_the_volume_to_a_pvc():
    docs = _store_docs(registry_storage_class="local-path")
    pod = next(d for d in docs if d["kind"] == "Pod")["spec"]
    volume = next(v for v in pod["volumes"] if v["name"] == rd.REGISTRY_VOLUME_NAME)
    assert volume["persistentVolumeClaim"]["claimName"] == rd.REGISTRY_VOLUME_NAME
    assert "hostPath" not in volume

    # The claim has to be created with the pod, and BEFORE it: a pod scheduled against a
    # claim that does not exist yet stays Pending with no explanation.
    assert docs[0]["kind"] == "PersistentVolumeClaim"
    assert docs[0]["spec"]["storageClassName"] == "local-path"
    assert rd.registry_pvc_manifest("default", "") is None


def test_the_registry_is_reachable_through_the_store_pods_service():
    """The Ingress routes to a Service port, so the port has to be published there."""
    service = next(d for d in _store_docs() if d["kind"] == "Service")
    ports = {p["name"]: p["port"] for p in service["spec"]["ports"]}

    assert ports["registry"] == rd.REGISTRY_PORT
    assert rd.registry_ingress_path()["backend"]["service"]["name"] == \
        service["metadata"]["name"]
    # And no longer on the service's own Service, which nothing routes /v2 at any more.
    assert rd.REGISTRY_PORT not in [
        p["port"] for p in sd._service_manifest("default")["spec"]["ports"]]


def test_the_infrastructure_containers_request_a_floor_not_an_estimate():
    """Requests are subtracted from what campaign jobs can be admitted against.

    And the asymmetry in the limits is deliberate: the index may not be OOMKilled during a
    bulk ingest at the end of an expensive campaign, and neither is CPU-capped, because a
    CPU limit throttles rather than fails and nobody attributes slow postprocessing to a
    cgroup.
    """
    from robovast.execution.cluster_execution import index_deploy

    for resources in (rd.REGISTRY_RESOURCES, index_deploy.INDEX_RESOURCES):
        assert "cpu" not in resources.get("limits", {})
        assert resources["requests"]["cpu"].endswith("m")

    assert index_deploy.INDEX_RESOURCES["limits"]["memory"] == "2Gi", \
        "stock shared_buffers is 128MB and ingest bulk-COPYs millions of rows"


def test_deletes_are_enabled_so_a_rebuilt_image_can_be_reclaimed():
    env = {e["name"]: e["value"] for e in _registry_container(_pod())["env"]}
    assert env["REGISTRY_STORAGE_DELETE_ENABLED"] == "true"


def test_the_registry_reports_readiness_on_the_registry_api():
    container = _registry_container(_pod())
    assert container["readinessProbe"]["httpGet"]["path"] == "/v2/"
    assert container["readinessProbe"]["httpGet"]["port"] == rd.REGISTRY_PORT


def test_the_setup_help_quotes_the_real_default_storage_path():
    """The help text spells the default out, and click evaluates it at import time so it
    cannot interpolate the constant. Pin the two together rather than let the documented
    default drift from the one the code uses."""
    from pathlib import Path

    cli = (Path(__file__).resolve().parents[2] / "src" / "robovast_cluster"
           / "robovast" / "execution" / "cluster_execution" / "cli.py").read_text()
    assert f"(default: {rd.DEFAULT_REGISTRY_HOST_PATH})" in cli


def test_the_ingress_path_and_the_prefix_describe_the_same_registry():
    """A registry answers at ``/v2`` of its host, which is why the prefix is a bare host
    with no path -- if these two ever disagreed, pushes and pulls would address
    different places."""
    assert rd.REGISTRY_INGRESS_PATH == "/v2"
    assert rd.registry_prefix("h.example.org") == "h.example.org"
    assert rd.registry_ingress_path()["backend"]["service"]["port"]["number"] == \
        rd.REGISTRY_PORT
    assert rd.registry_ingress_path()["backend"]["service"]["name"] == \
        store_pod.STORE_SERVICE_NAME
