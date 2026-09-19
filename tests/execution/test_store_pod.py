# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The ``robovast`` pod carries the deployment's setup-lifetime infrastructure.

The registry and the campaign index live in a pod ``vast cluster setup`` creates once,
rather than in ``robovast-service``, a Deployment every upgrade rolls. These tests pin what
that arrangement has to hold: one Service name that the DSN and the Ingress rule both agree
with, the same pod on every provider, and -- the ones an operator meets -- a loud refusal
on a cluster whose live pod does not match: one lacking a container, one still carrying an
object store, or one on a node the placement does not want.
"""

import types

import pytest

from robovast.execution.cluster_execution import (index_deploy, registry_deploy,
                                                  service_deploy, store_pod)


def _rke2_docs(namespace="default", **kwargs):
    return store_pod.attach_infrastructure([], namespace, **kwargs)


def test_every_provider_deploys_the_same_pod():
    """The pod is the same server doing the same work wherever it runs.

    A provider differs in what the cluster can say about itself and in how its README says
    to back the volumes, never in the pod: a container present on one provider and not on
    another is a deployment whose DSN and Ingress route are right in one place and wrong in
    another.
    """
    from robovast.execution.cluster_config import azure, gcp, minikube, rke2

    placement = {"namespace": "robotics", "index_storage_class": "fast",
                 "registry_authenticated": True, "control_node_labels": {"n": "1"}}
    manifests = [cls().store_pod_manifest(**placement) for cls in (
        rke2.Rke2ClusterConfig, minikube.MinikubeClusterConfig,
        azure.AzureClusterConfig, gcp.GcpClusterConfig)]

    assert all(m == manifests[0] for m in manifests)
    pod = next(d for d in manifests[0] if d["kind"] == "Pod")
    assert [c["name"] for c in pod["spec"]["containers"]] == list(
        store_pod.infrastructure_container_names())
    assert pod["spec"]["nodeSelector"] == {"n": "1"}


def test_the_pod_carries_the_registry_and_the_index_and_nothing_else():
    """Campaigns are on the service's results volume; nothing here holds them."""
    docs = store_pod.attach_infrastructure([], "robotics")
    pod = next(d for d in docs if d["kind"] == "Pod")
    service = next(d for d in docs if d["kind"] == "Service")

    assert [c["name"] for c in pod["spec"]["containers"]] == list(
        store_pod.infrastructure_container_names())
    assert store_pod.OBJECT_STORE_CONTAINER_NAME not in store_pod.infrastructure_container_names()
    assert pod["metadata"]["labels"] == service["spec"]["selector"]
    assert service["metadata"]["name"] == store_pod.STORE_SERVICE_NAME


def test_the_index_can_be_put_on_a_volume_of_its_own():
    """On a node pool whose machines are replaced -- which is every managed one -- a hostPath
    index goes with the node, so its class is its own argument."""
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


def test_a_published_registry_authenticates_and_still_probes(caplog):
    """The registry shares its hostname with the token-gated UI and has no gate of its own,
    so publishing without this serves an anonymous push/pull registry on a public name.

    The probe is half the change: it used to read ``/v2/``, which answers 401 once auth is
    on, and an httpGet probe counts that as a failure -- the container would never become
    Ready and nothing in the message would mention authentication.
    """
    docs = store_pod.attach_infrastructure([], "robotics", registry_authenticated=True)
    pod = next(d for d in docs if d["kind"] == "Pod")
    registry = next(c for c in pod["spec"]["containers"]
                    if c["name"] == registry_deploy.REGISTRY_CONTAINER_NAME)
    env = {e["name"]: e["value"] for e in registry["env"]}

    assert env["REGISTRY_AUTH"] == "htpasswd"
    assert env["REGISTRY_AUTH_HTPASSWD_PATH"].startswith(registry_deploy.REGISTRY_AUTH_DIR)
    assert any(m["name"] == registry_deploy.REGISTRY_AUTH_VOLUME_NAME
               for m in registry["volumeMounts"])
    assert any(v.get("secret", {}).get("secretName")
               == registry_deploy.REGISTRY_HTPASSWD_SECRET_NAME
               for v in pod["spec"]["volumes"])
    for probe in ("readinessProbe", "livenessProbe"):
        assert registry[probe]["httpGet"]["path"] == "/debug/health"
        assert registry[probe]["httpGet"]["port"] == registry_deploy.REGISTRY_DEBUG_PORT


def test_an_unpublished_registry_is_left_open_and_mounts_no_secret():
    """There is no route to it and no prefix to build into, so there is nothing to protect
    and no credential to invent."""
    docs = store_pod.attach_infrastructure([], "robotics")
    pod = next(d for d in docs if d["kind"] == "Pod")
    registry = next(c for c in pod["spec"]["containers"]
                    if c["name"] == registry_deploy.REGISTRY_CONTAINER_NAME)

    assert "REGISTRY_AUTH" not in {e["name"] for e in registry["env"]}
    assert not any(v["name"] == registry_deploy.REGISTRY_AUTH_VOLUME_NAME
                   for v in pod["spec"]["volumes"])


def test_the_password_file_is_bcrypt_because_nothing_else_is_accepted():
    """registry:2 verifies with Go's bcrypt and does not fall back to a weaker hash: it
    fails every request instead, so there is no quieter way to get this wrong."""
    entry = registry_deploy.htpasswd_entry("s3cret")
    user, _, digest = entry.partition(":")

    assert user == registry_deploy.REGISTRY_AUTH_USER
    assert digest.startswith("$2")
    assert "s3cret" not in entry


def test_the_registrys_ingress_backend_is_annotated_like_the_services_own():
    """The GKE Ingress fronts two Services, and reaches neither as a plain ClusterIP.

    The registry answers on the store pod's Service, so a `gce` Ingress whose /v2 rule names
    it needs container-native load balancing there too. Without it that backend never becomes
    healthy and the reason is in the load balancer, not in anything RoboVAST prints -- while
    the UI on `/` works, because the other Service was annotated.
    """
    service = next(d for d in store_pod.attach_infrastructure([], "robotics",
                                                              ingress_class="gce")
                   if d["kind"] == "Service")

    assert service["metadata"]["annotations"] == registry_deploy.NEG_ANNOTATION


def test_no_ingress_class_leaves_a_gke_specific_key_off_every_other_cluster():
    service = next(d for d in store_pod.attach_infrastructure([], "robotics")
                   if d["kind"] == "Service")

    assert "annotations" not in service["metadata"]
    assert registry_deploy.ingress_backend_annotations("nginx") == {}


def test_attaching_twice_changes_nothing():
    """Setup is re-runnable, and every provider renders its manifest fresh each time."""
    once = _rke2_docs()
    twice = store_pod.attach_infrastructure(once, "default")

    assert twice == once


def test_one_service_carries_every_port():
    """A second Service would duplicate the selector and add a name the DSN must match."""
    services = [d for d in _rke2_docs() if d["kind"] == "Service"]

    assert len(services) == 1
    ports = {p["name"] for p in services[0]["spec"]["ports"]}
    assert ports == {"registry", "index"}


def test_the_dsn_and_the_ingress_backend_name_the_same_service():
    """The two consumers of this pod's address, derived rather than written twice."""
    assert index_deploy.index_host("ns") == store_pod.store_host("ns")
    assert registry_deploy.registry_ingress_path()["backend"]["service"]["name"] == \
        store_pod.STORE_SERVICE_NAME
    assert "cluster.local" not in store_pod.store_host("ns"), \
        "the cluster domain is site configuration, not something to write into source"


def _pod(*names, node_selector=None, node_name="node-a"):
    """A live pod as the API returns one: named containers and a placement."""
    containers = [types.SimpleNamespace(name=n, env=[]) for n in names]
    return types.SimpleNamespace(spec=types.SimpleNamespace(
        containers=containers, node_selector=node_selector, node_name=node_name))


def _live(monkeypatch, pod):
    from kubernetes import client as kclient

    monkeypatch.setattr(service_deploy, "_load_kube_config", lambda *a, **k: None)

    def read(self, name, namespace):
        if pod is None:
            raise kclient.exceptions.ApiException(status=404)
        return pod
    monkeypatch.setattr(kclient, "CoreV1Api",
                        lambda *a, **k: type("C", (), {"read_namespaced_pod": read})())


def test_a_pod_lacking_a_container_is_named_not_guessed():
    assert store_pod.missing_infrastructure(_pod("index")) == [
        registry_deploy.REGISTRY_CONTAINER_NAME]
    assert store_pod.missing_infrastructure(_pod("registry", "index")) == []
    assert store_pod.missing_infrastructure(None) == list(
        store_pod.infrastructure_container_names())


def test_a_pod_lacking_a_container_is_refused_rather_than_half_deployed(monkeypatch):
    """`apply_manifests` keeps a live pod on a 409, so setup cannot add containers.

    Deploying anyway would leave an Ingress routing ``/v2`` at a container that is not
    there and a DSN naming a port nothing listens on -- an ImagePullBackOff on the next
    campaign and an IndexUnreachableError on the next query, neither of them near here.
    """
    _live(monkeypatch, _pod("index"))

    with pytest.raises(RuntimeError, match="cluster cleanup") as excinfo:
        service_deploy.verify_store_pod_infrastructure("default")
    assert registry_deploy.REGISTRY_CONTAINER_NAME in str(excinfo.value)
    assert "NOT migrated" not in str(excinfo.value), (
        "nothing durable is in this pod; the campaigns are on the results volume")


def test_a_pod_still_carrying_an_object_store_is_refused_and_says_what_it_costs(monkeypatch):
    """Campaigns live on the results volume, so a store in the pod is one nothing reads.

    The remedy recreates the pod, and the campaigns in that store go with it: the message
    has to say so, and name the archive commands, because nothing else holds a copy.
    """
    _live(monkeypatch, _pod(store_pod.OBJECT_STORE_CONTAINER_NAME, "registry", "index"))

    with pytest.raises(RuntimeError) as excinfo:
        service_deploy.verify_store_pod_infrastructure("default")
    message = str(excinfo.value)
    assert "vast cluster cleanup" in message and "vast cluster setup" in message
    assert "NOT migrated" in message
    assert "vast share" in message and "vast campaign download" in message


def test_no_pod_at_all_is_refused_rather_than_passed():
    """Absent is a cluster that is not set up, not one with nothing to check."""
    assert store_pod.carries_an_object_store(None) is False


def test_no_pod_at_all_names_setup(monkeypatch):
    _live(monkeypatch, None)

    with pytest.raises(RuntimeError, match="vast cluster setup"):
        service_deploy.verify_store_pod_infrastructure("default")


def test_a_matching_pod_passes(monkeypatch):
    _live(monkeypatch, _pod("registry", "index"))

    service_deploy.verify_store_pod_infrastructure("default")


# -- a pod the placement does not want ---------------------------------------------------

def test_a_pod_on_another_node_is_refused_before_the_apply(monkeypatch):
    """A changed nodeSelector never reaches a kept pod, so announcing it would be a lie."""
    _live(monkeypatch, _pod("registry", "index", node_selector={"kubernetes.io/hostname": "b"}))

    with pytest.raises(RuntimeError, match="cannot be moved") as excinfo:
        store_pod.refuse_a_pod_on_the_wrong_node("default", {"robovast.io/data-node": "true"})
    assert "not in this pod" in str(excinfo.value), (
        "the campaigns are on the results volume; recreating this pod does not touch them")


def test_a_pod_already_where_it_should_be_is_not_refused(monkeypatch):
    _live(monkeypatch, _pod("registry", "index",
                            node_selector={"robovast.io/data-node": "true", "pool": "x"}))

    store_pod.refuse_a_pod_on_the_wrong_node("default", {"robovast.io/data-node": "true"})


def test_no_pod_and_no_placement_are_never_refused(monkeypatch):
    _live(monkeypatch, None)

    store_pod.refuse_a_pod_on_the_wrong_node("default", {"robovast.io/data-node": "true"})
    store_pod.refuse_a_pod_on_the_wrong_node("default", None)


# -- auth that was configured but never reached the cluster ----------------------------

def _live_pod(*containers):
    return types.SimpleNamespace(spec=types.SimpleNamespace(containers=list(containers)))


def _live_container(name, env_names=()):
    return types.SimpleNamespace(
        name=name,
        env=[types.SimpleNamespace(name=n, value="x") for n in env_names])


def test_a_kept_pod_is_seen_to_be_serving_an_open_registry():
    """The 409 that keeps an existing pod keeps its container spec, so turning auth on
    in the manifest does not turn it on in the cluster.

    Without noticing this, setup mints a credential, writes both Secrets and reports
    success over a registry still serving anonymous pushes -- claiming to have closed a hole
    it left open, which is worse than never having claimed to.
    """
    open_registry = _live_pod(_live_container(registry_deploy.REGISTRY_CONTAINER_NAME,
                                              ["REGISTRY_STORAGE_DELETE_ENABLED"]))

    assert store_pod.registry_enforces_auth(open_registry) is False


def test_a_recreated_pod_is_seen_to_enforce_it():
    closed = _live_pod(_live_container(registry_deploy.REGISTRY_CONTAINER_NAME,
                                       ["REGISTRY_STORAGE_DELETE_ENABLED", "REGISTRY_AUTH"]))

    assert store_pod.registry_enforces_auth(closed) is True


def test_a_pod_without_a_registry_is_not_read_as_authenticating():
    """Absent and open are both "not asking for a credential", and the missing container is
    already reported by its own check."""
    assert store_pod.registry_enforces_auth(None) is False
    assert store_pod.registry_enforces_auth(_live_pod(_live_container("index"))) is False
