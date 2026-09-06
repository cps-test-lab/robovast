# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The ``robovast`` pod carries the deployment's setup-lifetime infrastructure.

The registry and the campaign index used to be extra containers in ``robovast-service``, a
Deployment every upgrade rolls. These tests pin what the move has to preserve: one Service
name that the DSN and the Ingress rule both agree with, a pod that exists on every provider
including the ones with no object store of their own, and -- the one an operator meets --
a loud refusal on a cluster whose store pod predates the move.
"""

import io

import pytest
import yaml

from robovast.execution.cluster_execution import (index_deploy, registry_deploy,
                                                  service_deploy, store_pod)


def _rke2_docs(namespace="default", **kwargs):
    from robovast.execution.cluster_config.rke2 import MINIO_MANIFEST_RKE2

    return store_pod.attach_infrastructure(
        list(yaml.safe_load_all(io.StringIO(MINIO_MANIFEST_RKE2))), namespace, **kwargs)


def test_a_provider_without_an_object_store_still_gets_the_pod():
    """GCS keeps campaign data in a bucket, but the registry and the index are pods.

    Without this the DSN and the Ingress backend would have to differ per provider -- one
    more thing that can be right in one place and wrong in another.
    """
    docs = store_pod.attach_infrastructure([], "robotics")
    pod = next(d for d in docs if d["kind"] == "Pod")
    service = next(d for d in docs if d["kind"] == "Service")

    assert [c["name"] for c in pod["spec"]["containers"]] == list(
        store_pod.infrastructure_container_names())
    assert pod["metadata"]["labels"] == service["spec"]["selector"]
    assert service["metadata"]["name"] == store_pod.STORE_SERVICE_NAME


def test_a_bucket_backed_provider_can_put_the_index_on_a_volume():
    """The one piece of this deployment's durable state a bucket does not hold.

    With the campaigns in a bucket there is no --store-class to back the index beside, so on a
    node pool whose machines are replaced -- which is every managed one -- a hostPath index
    goes with the node while every campaign it indexed survives.
    """
    docs = store_pod.attach_infrastructure([], "robotics",
                                           index_storage_class="premium-rwo",
                                           index_storage_size="100Gi")
    claim = next(d for d in docs if d["kind"] == "PersistentVolumeClaim")
    pod = next(d for d in docs if d["kind"] == "Pod")
    volume = next(v for v in pod["spec"]["volumes"]
                  if v["name"] == index_deploy.INDEX_VOLUME_NAME)

    assert claim["spec"]["storageClassName"] == "premium-rwo"
    assert claim["spec"]["resources"]["requests"]["storage"] == "100Gi"
    assert volume["persistentVolumeClaim"]["claimName"] == index_deploy.INDEX_VOLUME_NAME
    assert "hostPath" not in volume
    assert docs.index(claim) < docs.index(pod), (
        "a pod scheduled against a claim that does not exist yet stays Pending")


def test_attaching_twice_changes_nothing():
    """Setup is re-runnable, and every provider parses its manifest fresh each time."""
    once = _rke2_docs()
    twice = store_pod.attach_infrastructure(once, "default")

    assert twice == once


def test_one_service_carries_every_port():
    """A second Service would duplicate the selector and add a name the DSN must match."""
    services = [d for d in _rke2_docs() if d["kind"] == "Service"]

    assert len(services) == 1
    ports = {p["name"] for p in services[0]["spec"]["ports"]}
    assert ports == {"s3", "console", "registry", "index"}


def test_the_dsn_and_the_ingress_backend_name_the_same_service():
    """The two consumers of this pod's address, derived rather than written twice."""
    assert index_deploy.index_host("ns") == store_pod.store_host("ns")
    assert registry_deploy.registry_ingress_path()["backend"]["service"]["name"] == \
        store_pod.STORE_SERVICE_NAME
    assert "cluster.local" not in store_pod.store_host("ns"), \
        "the cluster domain is site configuration, not something to write into source"


class _Pod:
    """A store pod as the API returns one: named containers, and the store's volume.

    The backing is part of the fixture because the refusal reports a different cost per
    kind -- a pod without one would only ever exercise the branch that says nothing is
    lost, which is the reassurance that must not be given by default.
    """

    def __init__(self, *names, store="emptyDir", detail=None):
        mounts = {service_deploy.STORE_CONTAINER_NAME:
                  [type("M", (), {"name": "minio-storage",
                                  "mount_path": service_deploy.STORE_DATA_MOUNT})()]}
        containers = [type("C", (), {"name": n, "volume_mounts": mounts.get(n, [])})()
                      for n in names]
        volume = type("V", (), {"name": "minio-storage", "host_path": None,
                                "persistent_volume_claim": None, "empty_dir": None})()
        if store == "emptyDir":
            volume.empty_dir = object()
        elif store == "hostPath":
            volume.host_path = type("H", (), {"path": detail})()
        elif store == "claim":
            volume.persistent_volume_claim = type("P", (), {"claim_name": detail})()
        self.spec = type("S", (), {"containers": containers,
                                   "volumes": [volume] if store else []})()


def test_a_store_pod_that_predates_the_move_is_named_not_guessed():
    assert store_pod.missing_infrastructure(_Pod("minio")) == [
        registry_deploy.REGISTRY_CONTAINER_NAME, index_deploy.INDEX_CONTAINER_NAME]
    assert store_pod.missing_infrastructure(_Pod("minio", "registry", "index")) == []
    assert store_pod.missing_infrastructure(None) == list(
        store_pod.infrastructure_container_names())


def test_an_existing_cluster_is_refused_rather_than_half_migrated(monkeypatch):
    """`apply_manifests` keeps a live store pod on a 409, so setup cannot add containers.

    Deploying anyway would leave an Ingress routing ``/v2`` at a container that is not
    there and a DSN naming a port nothing listens on -- an ImagePullBackOff on the next
    campaign and an IndexUnreachableError on the next query, neither of them near here.
    """
    from kubernetes import client as kclient

    monkeypatch.setattr(service_deploy, "_load_kube_config", lambda *a, **k: None)
    monkeypatch.setattr(kclient, "CoreV1Api", lambda *a, **k: type(
        "C", (), {"read_namespaced_pod": lambda self, n, ns: _Pod("minio")})())

    with pytest.raises(RuntimeError, match="cluster cleanup"):
        service_deploy.verify_store_pod_infrastructure("default")


def test_the_refusal_names_what_recreating_the_pod_costs(monkeypatch):
    """The remedy recreates the store pod, and what that costs depends on the backing.

    An ``emptyDir`` store goes with the pod, so the campaigns must be archived first and
    the index cannot be re-ingested afterwards; a durable one survives, and saying
    otherwise would train an operator to ignore the warning that matters.
    """
    from kubernetes import client as kclient

    monkeypatch.setattr(service_deploy, "_load_kube_config", lambda *a, **k: None)

    def _refusal(pod):
        monkeypatch.setattr(kclient, "CoreV1Api", lambda *a, **k: type(
            "C", (), {"read_namespaced_pod": lambda self, n, ns: pod})())
        with pytest.raises(RuntimeError) as excinfo:
            service_deploy.verify_store_pod_infrastructure("default")
        return str(excinfo.value)

    ephemeral = _refusal(_Pod("minio"))
    assert "DISCARDS" in ephemeral
    assert "vast share" in ephemeral

    durable = _refusal(_Pod("minio", store="hostPath", detail="/var/lib/robovast-store"))
    assert "DISCARDS" not in durable
    assert "/var/lib/robovast-store" in durable

    claimed = _refusal(_Pod("minio", store="claim", detail="robovast-pvc"))
    assert "DISCARDS" not in claimed
    assert "robovast-pvc" in claimed


def test_a_store_backed_by_a_bucket_loses_nothing_with_the_pod():
    """No embedded store means the campaigns are in a bucket, which no pod holds."""
    assert service_deploy.store_backing(_Pod("registry", "index", store=None)) == (None, None)


def test_a_migrated_cluster_passes(monkeypatch):
    from kubernetes import client as kclient

    monkeypatch.setattr(service_deploy, "_load_kube_config", lambda *a, **k: None)
    monkeypatch.setattr(kclient, "CoreV1Api", lambda *a, **k: type(
        "C", (), {"read_namespaced_pod":
                  lambda self, n, ns: _Pod("minio", "registry", "index")})())

    service_deploy.verify_store_pod_infrastructure("default")
