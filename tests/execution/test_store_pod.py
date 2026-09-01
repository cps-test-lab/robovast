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
    def __init__(self, *names):
        self.spec = type("S", (), {"containers": [type("C", (), {"name": n})()
                                                  for n in names]})()


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


def test_a_migrated_cluster_passes(monkeypatch):
    from kubernetes import client as kclient

    monkeypatch.setattr(service_deploy, "_load_kube_config", lambda *a, **k: None)
    monkeypatch.setattr(kclient, "CoreV1Api", lambda *a, **k: type(
        "C", (), {"read_namespaced_pod":
                  lambda self, n, ns: _Pod("minio", "registry", "index")})())

    service_deploy.verify_store_pod_infrastructure("default")
