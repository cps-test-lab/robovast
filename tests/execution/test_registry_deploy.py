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


def test_the_registry_reports_readiness_where_auth_cannot_answer_401():
    """``/v2/`` answers 401 on a published deployment, and an httpGet probe reads anything
    outside 200-399 as a failure -- so probing the API the auth guards would leave the
    container permanently unready, with nothing in the message naming authentication.

    ``/debug/health`` is the registry's own unauthenticated health endpoint, on a port no
    Service publishes, so it is reachable by the kubelet and by nothing else.
    """
    container = _registry_container(_pod())
    assert container["readinessProbe"]["httpGet"]["path"] == "/debug/health"
    assert container["readinessProbe"]["httpGet"]["port"] == rd.REGISTRY_DEBUG_PORT
    assert rd.REGISTRY_DEBUG_PORT != rd.REGISTRY_PORT


def test_the_setup_help_quotes_the_real_default_storage_path():
    """The documented default is the constant itself, not a literal kept in step with it.

    Asserted against the rendered help rather than the source, so it stays true of what an
    operator is actually told when they ask.
    """
    import click

    from robovast.execution.cluster_execution.cli import setup

    help_text = {opt.opts[0]: (opt.help or "")
                 for opt in setup.params if isinstance(opt, click.Option)}
    assert rd.DEFAULT_REGISTRY_HOST_PATH in help_text["--registry-path"]


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


# -- the credential, in its two representations ---------------------------------------

def _auths(secret):
    import json
    # stringData, not data: the manifest is what setup hands the API server, so the value
    # here is plaintext and is base64-encoded by Kubernetes rather than by us.
    return json.loads(secret["stringData"][".dockerconfigjson"]).get("auths", {})


def test_the_builtin_registrys_credential_reaches_every_client_through_one_secret(monkeypatch):
    """The build Job's push, a campaign pod's imagePullSecrets, the image warmer and the
    service's own probe all name this one Secret and nothing else, so the credential has to
    arrive in it or none of them can authenticate."""
    for var in ("ROBOVAST_REGISTRY_SERVER", "ROBOVAST_REGISTRY_USERNAME",
                "ROBOVAST_REGISTRY_PASSWORD"):
        monkeypatch.delenv(var, raising=False)

    secret = sd._registry_dockerconfig_manifest(  # pylint: disable=protected-access
        "default", builtin_host="robovast.example.org", builtin_password="s3cret")
    entry = _auths(secret)["robovast.example.org"]

    assert entry["username"] == rd.REGISTRY_AUTH_USER
    assert entry["password"] == "s3cret"
    assert secret["metadata"]["name"] == sd.REGISTRY_PUSH_SECRET_NAME


def test_an_external_registry_and_the_builtin_one_coexist(monkeypatch):
    """A dockerconfigjson is keyed by host, so neither has to know about the other -- and
    configuring an external registry must not silently disable pulls of our own images."""
    monkeypatch.setenv("ROBOVAST_REGISTRY_SERVER", "ghcr.io")
    monkeypatch.setenv("ROBOVAST_REGISTRY_USERNAME", "someone")
    monkeypatch.setenv("ROBOVAST_REGISTRY_PASSWORD", "pat")

    auths = _auths(sd._registry_dockerconfig_manifest(  # pylint: disable=protected-access
        "default", builtin_host="robovast.example.org", builtin_password="s3cret"))

    assert set(auths) == {"ghcr.io", "robovast.example.org"}


def test_an_unpublished_deployment_mints_no_registry_credential(monkeypatch):
    """No route to the registry and no prefix to build into, so there is nothing to protect
    and nothing to invent. Returning a password here would also write a password file the
    registry was never told to read."""
    for var in ("ROBOVAST_REGISTRY_SERVER", "ROBOVAST_REGISTRY_USERNAME",
                "ROBOVAST_REGISTRY_PASSWORD"):
        monkeypatch.delenv(var, raising=False)

    assert sd.ensure_registry_htpasswd("default", None, host="") == ""
    assert sd._registry_dockerconfig_manifest("default") is None  # pylint: disable=protected-access
