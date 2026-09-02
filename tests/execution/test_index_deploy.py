# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The Postgres that indexes every campaign, and what it must never do.

These are manifest tests -- no cluster -- because the failures they guard against are
manifest facts: an ``emptyDir`` that discards the index on a restart, a password
regenerated against a data directory that already has one, a probe impatient enough to turn
a slow first start into a crash loop, and a DSN naming a host that does not exist.
"""

import io

import yaml

from robovast.execution.cluster_execution import index_deploy, service_deploy, store_pod


def _pod_spec(**kwargs):
    manifest = service_deploy._deployment_manifest(  # pylint: disable=protected-access
        "default", "robovast:test", restarted_at="fixed", **kwargs)
    return manifest["spec"]["template"]["spec"]


def _store_docs(namespace="default", **kwargs):
    from robovast.execution.cluster_config.rke2 import MINIO_MANIFEST_RKE2

    return store_pod.attach_infrastructure(
        list(yaml.safe_load_all(io.StringIO(MINIO_MANIFEST_RKE2))), namespace, **kwargs)


def _store_pod_spec(**kwargs):
    return next(d for d in _store_docs(**kwargs) if d["kind"] == "Pod")["spec"]


def test_the_index_runs_in_the_store_pod_not_the_service_pod():
    """The service Deployment is rolled by every upgrade; the store pod is not.

    Postgres there restarted on each version bump, including one that only bumped the
    controller image. The store pod is created once at setup, is node-pinned, and already
    holds the campaign data these rows index.
    """
    assert [c["name"] for c in _pod_spec()["containers"]] == [service_deploy.SERVICE_NAME]
    assert index_deploy.INDEX_CONTAINER_NAME in [
        c["name"] for c in _store_pod_spec()["containers"]]


def test_the_index_answers_on_the_store_pods_own_service():
    """One Service, one selector. A second object would duplicate both for no isolation."""
    services = [d for d in _store_docs() if d["kind"] == "Service"]

    assert len(services) == 1, "the store pod keeps exactly one Service"
    assert index_deploy.INDEX_PORT in [p["port"] for p in services[0]["spec"]["ports"]]


def test_the_index_port_is_not_on_the_services_own_service():
    """Nothing routes an Ingress at it, and the service pod no longer runs Postgres."""
    service = service_deploy._service_manifest("default", "")  # pylint: disable=protected-access

    ports = [p.get("port") for p in service["spec"]["ports"]]
    assert index_deploy.INDEX_PORT not in ports


def test_the_index_and_the_store_are_never_separated():
    """The index must not outlive the campaigns it was ingested from, and this is how.

    It shares the store's pod, takes the store's backing and sits at a path derived from the
    store's, so the two are created, moved and destroyed as one thing. An emptyDir here is the
    other way of arranging that, and it costs a full re-ingest on every restart of a store
    that now survives one.
    """
    volume = index_deploy.index_volume("/media/data/index")

    assert volume["hostPath"]["path"] == "/media/data/index"
    assert "emptyDir" not in volume, (
        "an index emptied on every restart re-ingests a corpus the store still holds")


def test_the_index_is_a_claim_exactly_where_the_store_is_one():
    volume = index_deploy.index_volume("/media/data/index", "local-path")

    assert volume["persistentVolumeClaim"]["claimName"] == index_deploy.INDEX_VOLUME_NAME
    assert index_deploy.index_pvc_manifest("default", "local-path")["spec"][
        "storageClassName"] == "local-path"
    assert index_deploy.index_pvc_manifest("default", "") is None


def test_the_data_directory_is_below_the_mount_not_the_mount():
    """A PVC root often carries lost+found, and initdb refuses a non-empty directory.

    Getting this wrong is a CrashLoopBackOff whose log says only "directory exists but is
    not empty", which reads like a broken volume rather than a manifest mistake.
    """
    env = {e["name"]: e.get("value") for e in index_deploy.index_container()["env"]}

    assert env["PGDATA"] == index_deploy.INDEX_DATA_DIR
    assert env["PGDATA"].startswith(index_deploy.INDEX_MOUNT_DIR + "/")
    assert env["PGDATA"] != index_deploy.INDEX_MOUNT_DIR


def test_the_password_comes_from_a_secret_not_the_manifest():
    """``vast service manifests`` prints this; a password does not belong in a log."""
    env = {e["name"]: e for e in index_deploy.index_container()["env"]}

    ref = env["POSTGRES_PASSWORD"]["valueFrom"]["secretKeyRef"]
    assert ref["name"] == index_deploy.INDEX_SECRET_NAME
    assert "value" not in env["POSTGRES_PASSWORD"]


def test_readiness_asks_postgres_not_the_port():
    """A TCP port opens before recovery finishes, so a port check reports a false ready."""
    container = index_deploy.index_container()

    probe = container["readinessProbe"]
    assert "exec" in probe and "tcpSocket" not in probe
    assert probe["exec"]["command"][0] == "pg_isready"


def test_liveness_is_slower_than_readiness_so_a_slow_start_is_not_a_crash_loop():
    """A first start runs initdb; a restart after an unclean stop replays WAL."""
    container = index_deploy.index_container()

    assert (container["livenessProbe"]["initialDelaySeconds"]
            > container["readinessProbe"]["initialDelaySeconds"])
    assert container["livenessProbe"]["failureThreshold"] > 1


def test_the_dsn_names_the_store_service_in_this_namespace():
    """A different pod now, so the host is a Service name -- assembled, never configured.

    ``<service>.<namespace>.svc`` resolves in any cluster through the pod's own search
    domain, so no cluster domain and no site-specific host is written into the source. The
    ``.svc`` suffix matters: a bare name would resolve through the *client's* namespace.
    """
    dsn = index_deploy.index_dsn("pw", "robotics")

    assert "host=robovast.robotics.svc" in dsn
    assert "127.0.0.1" not in dsn and "localhost" not in dsn
    assert f"dbname={index_deploy.INDEX_DB_NAME}" in dsn
    assert index_deploy.index_host("robotics") == store_pod.store_host("robotics")


def test_the_role_is_not_the_postgres_superuser():
    """A superuser connection makes the read-only reader role a formality."""
    assert index_deploy.INDEX_DB_USER != "postgres"


def test_the_service_is_told_where_its_index_is():
    """One spelling of the env var, shared with common.index_db."""
    from robovast.common.index_db import DSN_ENV

    manifests = service_deploy.service_manifests(
        namespace="default", image="robovast:test", index_password="pw")
    deployment = next(m for m in manifests if m["kind"] == "Deployment")
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    env = {e["name"]: e.get("value") for e in container["env"]}

    assert DSN_ENV == service_deploy.INDEX_DSN_ENV
    assert "host=robovast.default.svc" in env[DSN_ENV]
    assert "password=pw" in env[DSN_ENV]


def test_the_secret_is_emitted_when_a_password_is_given():
    manifests = service_deploy.service_manifests(
        namespace="default", image="robovast:test", index_password="pw")

    secret = next((m for m in manifests if m["kind"] == "Secret"
                   and m["metadata"]["name"] == index_deploy.INDEX_SECRET_NAME), None)
    assert secret is not None
    assert secret["stringData"][index_deploy.INDEX_PASSWORD_KEY] == "pw"


def test_an_explicit_dsn_in_env_is_not_overridden():
    """A deployment pointing at an external Postgres must keep pointing at it."""
    from robovast.common.index_db import DSN_ENV

    manifests = service_deploy.service_manifests(
        namespace="default", image="robovast:test", index_password="pw",
        env=[{"name": DSN_ENV, "value": "host=elsewhere port=5432 dbname=rv"}])
    deployment = next(m for m in manifests if m["kind"] == "Deployment")
    env = {e["name"]: e.get("value")
           for e in deployment["spec"]["template"]["spec"]["containers"][0]["env"]}

    assert env[DSN_ENV] == "host=elsewhere port=5432 dbname=rv"


def test_a_storage_path_places_the_index():
    """The path a caller threads through must reach the manifest.

    While it was accepted and discarded, a manifest could name a directory nothing wrote to
    and still read as durable storage -- which is the failure an accepted-and-ignored argument
    invites.
    """
    volume = next(v for v in _store_pod_spec(index_storage_path="/mnt/big/robovast-index")
                  ["volumes"] if v["name"] == index_deploy.INDEX_VOLUME_NAME)

    assert volume["hostPath"]["path"] == "/mnt/big/robovast-index"
    assert "emptyDir" not in volume
