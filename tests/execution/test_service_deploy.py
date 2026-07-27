# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for robovast-service deployment manifest generation.

These check the manifest *shapes* without a cluster. Server-side validation
against a real API server is exercised via ``deploy_service(dry_run=True)`` in
the cluster e2e path, not here.
"""

import pytest

from robovast.execution.cluster_execution import service_deploy as sd


@pytest.fixture(autouse=True)
def _no_host_secrets(monkeypatch):
    """Keep manifest shape deterministic regardless of a CI GITHUB_TOKEN / share config."""
    for var in sd._GIT_TOKEN_HOST_ENVS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("ROBOVAST_SHARE_TYPE", raising=False)
    for var in ("ROBOVAST_NTFY_TOPIC", "ROBOVAST_NTFY_SERVER", "ROBOVAST_NTFY_TOKEN"):
        monkeypatch.delenv(var, raising=False)


def test_manifests_have_expected_kinds_and_names():
    ms = sd.service_manifests(namespace="default", image="example/robovast:test")
    kinds = [(m["kind"], m["metadata"]["name"]) for m in ms]
    assert kinds == [
        ("ServiceAccount", sd.SERVICE_ACCOUNT),
        ("Role", sd.SERVICE_ACCOUNT),
        ("RoleBinding", sd.SERVICE_ACCOUNT),
        # Cluster-scoped read for the /usage endpoint (nodes are not namespaced).
        ("ClusterRole", f"{sd.SERVICE_ACCOUNT}-usage-default"),
        ("ClusterRoleBinding", f"{sd.SERVICE_ACCOUNT}-usage-default"),
        ("Deployment", sd.SERVICE_NAME),
        ("Service", sd.SERVICE_NAME),
    ]
    # No git token → no Secret injected.
    assert not any(m["kind"] == "Secret" for m in ms)


def test_git_token_injects_secret_and_file_mount_not_env():
    ms = sd.service_manifests(namespace="default", image="x", git_token="ghp_example")
    secret = next(m for m in ms if m["kind"] == "Secret")
    assert secret["metadata"]["name"] == sd.GIT_SECRET_NAME
    assert secret["stringData"][sd.GIT_SECRET_KEY] == "ghp_example"

    dep = next(m for m in ms if m["kind"] == "Deployment")
    pod = dep["spec"]["template"]["spec"]
    container = pod["containers"][0]
    # The token is NOT exposed as an env var (would be inherited by children).
    assert not any(e["name"] == "ROBOVAST_GIT_TOKEN" for e in container["env"])
    # It is mounted read-only as a file at the path config_plugins reads.
    mount = next(m for m in container["volumeMounts"] if m["name"] == "git-credentials")
    assert mount["readOnly"] is True
    assert mount["mountPath"] == sd.GIT_TOKEN_MOUNT_DIR
    vol = next(v for v in pod["volumes"] if v["name"] == "git-credentials")
    assert vol["secret"]["secretName"] == sd.GIT_SECRET_NAME
    # Mount dir + secret key must equal the file config_plugins reads.
    from robovast.common.config_plugins import GIT_TOKEN_FILE
    assert f"{sd.GIT_TOKEN_MOUNT_DIR}/{sd.GIT_SECRET_KEY}" == GIT_TOKEN_FILE


def test_git_token_read_from_host_env(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_from_env")
    ms = sd.service_manifests(namespace="default", image="x")
    secret = next(m for m in ms if m["kind"] == "Secret")
    assert secret["stringData"][sd.GIT_SECRET_KEY] == "ghp_from_env"


def test_share_env_injects_secret_and_envfrom():
    # An explicit share_env is materialised into a Secret and pulled in via envFrom
    # (env vars — the in-driver upload reads them from os.environ).
    share_env = {"ROBOVAST_SHARE_TYPE": "gcs", "ROBOVAST_GCS_BUCKET": "b",
                 "ROBOVAST_GCS_KEY_JSON": "{}"}
    ms = sd.service_manifests(namespace="default", image="x", share_env=share_env)
    secret = next(m for m in ms if m["kind"] == "Secret")
    assert secret["metadata"]["name"] == sd.SHARE_SECRET_NAME
    assert secret["stringData"] == share_env

    dep = next(m for m in ms if m["kind"] == "Deployment")
    container = dep["spec"]["template"]["spec"]["containers"][0]
    assert container["envFrom"] == [{"secretRef": {"name": sd.SHARE_SECRET_NAME}}]


def test_share_env_read_from_host_provider(monkeypatch):
    # ROBOVAST_SHARE_TYPE + provider vars on the host → resolved via build_pod_env.
    monkeypatch.setenv("ROBOVAST_SHARE_TYPE", "webdav")
    monkeypatch.setenv("ROBOVAST_WEBDAV_URL", "https://dav.example/col")
    monkeypatch.setenv("ROBOVAST_WEBDAV_USER", "u")
    monkeypatch.setenv("ROBOVAST_WEBDAV_PASSWORD", "p")
    ms = sd.service_manifests(namespace="default", image="x")
    secret = next(m for m in ms if m["kind"] == "Secret")
    assert secret["metadata"]["name"] == sd.SHARE_SECRET_NAME
    data = secret["stringData"]
    assert data["ROBOVAST_SHARE_TYPE"] == "webdav"
    assert data["ROBOVAST_WEBDAV_PASSWORD"] == "p"


def test_unknown_share_type_fails_fast(monkeypatch):
    import click
    monkeypatch.setenv("ROBOVAST_SHARE_TYPE", "nope-not-a-provider")
    with pytest.raises(click.UsageError):
        sd.service_manifests(namespace="default", image="x")


def test_no_share_means_no_secret_or_envfrom():
    ms = sd.service_manifests(namespace="default", image="x")  # env cleared by fixture
    assert not any(m["kind"] == "Secret" for m in ms)
    dep = next(m for m in ms if m["kind"] == "Deployment")
    assert "envFrom" not in dep["spec"]["template"]["spec"]["containers"][0]


def test_ntfy_env_injects_secret_and_envfrom(monkeypatch):
    # ROBOVAST_NTFY_TOPIC (+ optional token) on the host → a ntfy-credentials Secret
    # pulled in via envFrom, so the in-service Notifier.from_env picks it up.
    monkeypatch.setenv("ROBOVAST_NTFY_TOPIC", "robovast-alice")
    monkeypatch.setenv("ROBOVAST_NTFY_TOKEN", "tk_xxx")
    ms = sd.service_manifests(namespace="default", image="x")
    secret = next(m for m in ms if m["kind"] == "Secret")
    assert secret["metadata"]["name"] == sd.NTFY_SECRET_NAME
    assert secret["stringData"] == {
        "ROBOVAST_NTFY_TOPIC": "robovast-alice", "ROBOVAST_NTFY_TOKEN": "tk_xxx"}

    dep = next(m for m in ms if m["kind"] == "Deployment")
    container = dep["spec"]["template"]["spec"]["containers"][0]
    assert container["envFrom"] == [{"secretRef": {"name": sd.NTFY_SECRET_NAME}}]


def test_no_ntfy_topic_means_no_secret_or_envfrom(monkeypatch):
    # Only the optional server set (no topic) → notifications disabled, no Secret.
    monkeypatch.setenv("ROBOVAST_NTFY_SERVER", "https://ntfy.sh")
    ms = sd.service_manifests(namespace="default", image="x")
    assert not any(m["kind"] == "Secret" for m in ms)
    dep = next(m for m in ms if m["kind"] == "Deployment")
    assert "envFrom" not in dep["spec"]["template"]["spec"]["containers"][0]


def test_share_and_ntfy_both_configured_carry_both_secretrefs(monkeypatch):
    monkeypatch.setenv("ROBOVAST_NTFY_TOPIC", "robovast-alice")
    share_env = {"ROBOVAST_SHARE_TYPE": "gcs", "ROBOVAST_GCS_BUCKET": "b"}
    ms = sd.service_manifests(namespace="default", image="x", share_env=share_env)
    secret_names = {m["metadata"]["name"] for m in ms if m["kind"] == "Secret"}
    assert secret_names == {sd.SHARE_SECRET_NAME, sd.NTFY_SECRET_NAME}

    dep = next(m for m in ms if m["kind"] == "Deployment")
    container = dep["spec"]["template"]["spec"]["containers"][0]
    assert container["envFrom"] == [
        {"secretRef": {"name": sd.SHARE_SECRET_NAME}},
        {"secretRef": {"name": sd.NTFY_SECRET_NAME}},
    ]


def test_no_git_token_means_no_volume_or_mount():
    ms = sd.service_manifests(namespace="default", image="x")  # env cleared by fixture
    dep = next(m for m in ms if m["kind"] == "Deployment")
    pod = dep["spec"]["template"]["spec"]
    assert "volumes" not in pod
    assert "volumeMounts" not in pod["containers"][0]


def test_deployment_runs_vast_serve_on_service_port():
    ms = sd.service_manifests(namespace="ns1", image="example/robovast:test")
    dep = next(m for m in ms if m["kind"] == "Deployment")
    container = dep["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == "example/robovast:test"
    assert container["command"] == ["vast", "serve", "--host", "0.0.0.0",
                                    "--port", str(sd.SERVICE_PORT)]
    assert container["ports"][0]["containerPort"] == sd.SERVICE_PORT
    assert container["readinessProbe"]["httpGet"]["path"] == "/healthz"
    # binds to the service account that can launch controllers
    assert dep["spec"]["template"]["spec"]["serviceAccountName"] == sd.SERVICE_ACCOUNT
    # namespace threaded through every object
    assert all(m["metadata"].get("namespace", "ns1") == "ns1"
               for m in ms if m["kind"] != "ClusterRole")


def test_deploy_context_stamped_into_service_env():
    # Per-cluster resource lists are keyed by kubeconfig context name; in-cluster
    # there is no kubeconfig, so deploy records the context for the in-pod driver.
    ms = sd.service_manifests(namespace="default", image="x", config_name="rke2",
                              kube_context="gcp-c4")
    dep = next(m for m in ms if m["kind"] == "Deployment")
    env = {e["name"]: e["value"] for e in
           dep["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["ROBOVAST_KUBE_CONTEXT"] == "gcp-c4"


def test_no_context_stamped_when_deploy_uses_active_context():
    ms = sd.service_manifests(namespace="default", image="x", config_name="rke2")
    dep = next(m for m in ms if m["kind"] == "Deployment")
    names = {e["name"] for e in
             dep["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert "ROBOVAST_KUBE_CONTEXT" not in names


def test_service_is_clusterip_selecting_the_deployment():
    ms = sd.service_manifests(namespace="default", image="x")
    svc = next(m for m in ms if m["kind"] == "Service")
    assert svc["spec"]["type"] == "ClusterIP"
    assert svc["spec"]["selector"] == {"app": sd.SERVICE_NAME}
    assert svc["spec"]["ports"][0]["port"] == sd.SERVICE_PORT


def test_service_rbac_can_manage_jobs_pods_and_exec():
    ms = sd.service_manifests(namespace="default", image="x")
    role = next(m for m in ms if m["kind"] == "Role")
    resources = {r for rule in role["rules"] for r in rule["resources"]}
    # The service drives campaigns in-process now (no controller pod), so it needs
    # everything that pod's ServiceAccount used to hold: it creates/monitors the
    # scenario + postprocessing Jobs, their pods/logs, and the per-campaign aux pods
    # it execs into.
    assert {"jobs", "jobs/status"} <= resources
    assert {"pods", "pods/log", "pods/exec"} <= resources


def test_default_image_resolves_from_controller_image():
    # No explicit image → falls back to resolve_controller_image()
    from robovast.common.execution import resolve_controller_image
    ms = sd.service_manifests(namespace="default")
    dep = next(m for m in ms if m["kind"] == "Deployment")
    assert dep["spec"]["template"]["spec"]["containers"][0]["image"] == \
        resolve_controller_image()
