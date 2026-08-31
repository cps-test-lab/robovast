# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The Postgres sidecar in the service pod, and what it must never do.

These are manifest tests -- no cluster -- because the failures they guard against are
manifest facts: an ``emptyDir`` that discards the index on a routine upgrade, a password
regenerated against a data directory that already has one, a probe impatient enough to turn
a slow first start into a crash loop.
"""

from robovast.execution.cluster_execution import index_deploy, service_deploy


def _pod_spec(**kwargs):
    manifest = service_deploy._deployment_manifest(  # pylint: disable=protected-access
        "default", "robovast:test", restarted_at="fixed", **kwargs)
    return manifest["spec"]["template"]["spec"]


def test_the_index_runs_in_the_service_pod():
    """Same pod, so the service reaches it on localhost with no Service object."""
    names = [c["name"] for c in _pod_spec()["containers"]]

    assert index_deploy.INDEX_CONTAINER_NAME in names
    assert names[0] == service_deploy.SERVICE_NAME, "the service stays the first container"


def test_the_index_port_is_not_published_anywhere():
    """A database on the network with one shared password is a different posture."""
    service = service_deploy._service_manifest("default", "")  # pylint: disable=protected-access

    ports = [p.get("port") for p in service["spec"]["ports"]]
    assert index_deploy.INDEX_PORT not in ports


def test_the_volume_is_never_an_emptydir():
    """The sharpest rule here. Every upgrade restarts this pod.

    With an emptyDir a routine version bump would drop the index, so every campaign would
    need re-ingesting from the object store to be queryable -- hours of work for the
    existing corpus, triggered by a deploy nobody thought was destructive.
    """
    for storage_class in ("", "local-path"):
        volume = index_deploy.index_volume(storage_class=storage_class)
        assert "emptyDir" not in volume, f"emptyDir offered for storage_class={storage_class!r}"
        assert "hostPath" in volume or "persistentVolumeClaim" in volume


def test_a_storage_class_claims_a_pvc_and_none_falls_back_to_hostpath():
    """A stock RKE2 ships no StorageClass, so a PVC there stays Pending forever."""
    assert index_deploy.index_volume(storage_class="local-path") == {
        "name": index_deploy.INDEX_VOLUME_NAME,
        "persistentVolumeClaim": {"claimName": index_deploy.INDEX_VOLUME_NAME}}

    assert index_deploy.index_volume(storage_path="/data/idx")["hostPath"]["path"] == "/data/idx"
    assert index_deploy.index_pvc_manifest("default", "") is None
    assert index_deploy.index_pvc_manifest("default", "local-path")["spec"][
        "storageClassName"] == "local-path"


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


def test_the_dsn_uses_an_ipv4_literal_not_localhost():
    """``localhost`` resolves to ``::1`` first in a dual-stack pod.

    Postgres' default ``listen_addresses`` covers IPv4 only, so the first attempt fails
    and the retry succeeds -- an intermittent start-up error that looks like a race.
    """
    dsn = index_deploy.index_dsn("pw")

    assert "host=127.0.0.1" in dsn
    assert "localhost" not in dsn
    assert f"dbname={index_deploy.INDEX_DB_NAME}" in dsn


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
    assert "host=127.0.0.1" in env[DSN_ENV]
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


def test_the_index_hostpath_follows_the_workspaces_store():
    """A deployer who moved the workspaces store moved the node-local data."""
    volume = next(v for v in _pod_spec(workspaces_storage_path="/mnt/big/robovast-workspaces")
                  ["volumes"] if v["name"] == index_deploy.INDEX_VOLUME_NAME)

    assert volume["hostPath"]["path"] == "/mnt/big/robovast-index"
