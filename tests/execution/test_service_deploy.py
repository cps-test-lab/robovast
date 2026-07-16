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
def _no_host_git_token(monkeypatch):
    """Keep manifest shape deterministic regardless of a CI GITHUB_TOKEN / .env."""
    monkeypatch.setattr(sd, "_load_setup_dotenv", lambda: None)
    for var in sd._GIT_TOKEN_HOST_ENVS:
        monkeypatch.delenv(var, raising=False)


def test_manifests_have_expected_kinds_and_names():
    ms = sd.service_manifests(namespace="default", image="example/robovast:test")
    kinds = [(m["kind"], m["metadata"]["name"]) for m in ms]
    assert kinds == [
        ("ServiceAccount", sd.SERVICE_ACCOUNT),
        ("Role", sd.SERVICE_ACCOUNT),
        ("RoleBinding", sd.SERVICE_ACCOUNT),
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


def test_service_is_clusterip_selecting_the_deployment():
    ms = sd.service_manifests(namespace="default", image="x")
    svc = next(m for m in ms if m["kind"] == "Service")
    assert svc["spec"]["type"] == "ClusterIP"
    assert svc["spec"]["selector"] == {"app": sd.SERVICE_NAME}
    assert svc["spec"]["ports"][0]["port"] == sd.SERVICE_PORT


def test_service_rbac_can_manage_pods_but_not_jobs():
    ms = sd.service_manifests(namespace="default", image="x")
    role = next(m for m in ms if m["kind"] == "Role")
    resources = {r for rule in role["rules"] for r in rule["resources"]}
    assert "pods" in resources and "pods/log" in resources
    # the service launches/monitors controller pods; controllers create the Jobs
    assert "jobs" not in resources


def test_default_image_resolves_from_controller_image():
    # No explicit image → falls back to resolve_controller_image()
    from robovast.common.execution import resolve_controller_image
    ms = sd.service_manifests(namespace="default")
    dep = next(m for m in ms if m["kind"] == "Deployment")
    assert dep["spec"]["template"]["spec"]["containers"][0]["image"] == \
        resolve_controller_image()
