# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The service's own pod pulls with the pull Secret every campaign pod resolves.

``resolve_pull_secret`` is the one resolution: a pull Secret named in the cluster config's
registry settings, else the Secret ``setup`` creates when it exists. A deployment whose
registry needs a differently named Secret must get it on the service pod as well, or the
service sits in ``ImagePullBackOff`` while the pods it creates pull fine.
"""

from unittest.mock import MagicMock

import pytest

from robovast.execution.cluster_execution import service_deploy


@pytest.fixture
def apis(monkeypatch):
    """Every API ``deploy_service`` reaches for, with the setup-created Secret present."""
    from kubernetes import client as kclient

    core, rbac, apps, net = (MagicMock(name=n) for n in ("core", "rbac", "apps", "net"))
    monkeypatch.setattr(kclient, "CoreV1Api", lambda *a, **k: core)
    monkeypatch.setattr(kclient, "RbacAuthorizationV1Api", lambda *a, **k: rbac)
    monkeypatch.setattr(kclient, "AppsV1Api", lambda *a, **k: apps)
    monkeypatch.setattr(kclient, "NetworkingV1Api", lambda *a, **k: net)
    from robovast.execution.cluster_execution import kube_client

    monkeypatch.setattr(kube_client, "load_kube_config", lambda *a, **k: None)
    monkeypatch.setattr(service_deploy, "_load_kube_config", lambda *a, **k: None)
    monkeypatch.setattr(service_deploy, "service_storage_from_cluster", lambda *a, **k: {})
    monkeypatch.setattr(service_deploy, "job_node_pool_from_cluster", lambda *a, **k: {})
    monkeypatch.setattr(service_deploy, "_resolve_data_node", lambda *a, **k: {})
    monkeypatch.setattr(service_deploy, "existing_auth_token", lambda *a, **k: "token")
    return apps


def _deployed_pull_secrets(apps):
    (deployment,) = [c.args[1] for c in apps.create_namespaced_deployment.call_args_list
                     if c.args[1]["metadata"]["name"] == "robovast-service"]
    return deployment["spec"]["template"]["spec"].get("imagePullSecrets")


def test_a_configured_pull_secret_reaches_the_service_pod(apis, monkeypatch):
    monkeypatch.setenv("ROBOVAST_REGISTRY_PULL_SECRET", "site-pull")
    service_deploy.deploy_service(namespace="default", config_name="rke2")

    assert _deployed_pull_secrets(apis) == [{"name": "site-pull"}]


def test_without_one_the_service_pod_takes_the_secret_setup_creates(apis, monkeypatch):
    monkeypatch.delenv("ROBOVAST_REGISTRY_PULL_SECRET", raising=False)
    service_deploy.deploy_service(namespace="default", config_name="rke2")

    assert _deployed_pull_secrets(apis) == [{"name": service_deploy.REGISTRY_PUSH_SECRET_NAME}]


def test_a_deploy_without_a_cluster_config_is_refused(apis):
    """The pull Secret is resolved from the config, so none means no resolution at all."""
    with pytest.raises(ValueError, match="cluster config"):
        service_deploy.deploy_service(namespace="default")
