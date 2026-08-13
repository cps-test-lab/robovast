# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Setup reports success only once the service can actually answer.

It used to return as soon as the Deployment object existed and print
"✓ Cluster setup completed successfully!". An image that could not be pulled therefore
surfaced one command later as a connection failure, pointing at the network instead of
at the ImagePullBackOff that had already happened. The pod's own reason is right there.
"""

from types import SimpleNamespace
from unittest import mock

import pytest

from robovast.execution.cluster_execution import service_deploy


def _deployment(ready):
    return SimpleNamespace(status=SimpleNamespace(ready_replicas=ready))


def _pod(created, waiting_reason=None, phase="Pending"):
    waiting = (SimpleNamespace(reason=waiting_reason, message="back-off pulling image")
               if waiting_reason else None)
    return SimpleNamespace(
        metadata=SimpleNamespace(creation_timestamp=created),
        status=SimpleNamespace(
            phase=phase,
            init_container_statuses=None,
            container_statuses=[SimpleNamespace(state=SimpleNamespace(waiting=waiting))],
        ))


@pytest.fixture(autouse=True)
def _no_kube(monkeypatch):
    monkeypatch.setattr(service_deploy, "_load_kube_config", lambda ctx=None: None)


def test_a_ready_replica_returns_immediately(monkeypatch):
    apps = mock.Mock()
    apps.read_namespaced_deployment_status.return_value = _deployment(1)
    monkeypatch.setattr("kubernetes.client.AppsV1Api", lambda: apps)
    monkeypatch.setattr("kubernetes.client.CoreV1Api", mock.Mock())

    service_deploy.wait_for_service_ready(timeout_s=5)


def test_a_pod_that_never_starts_reports_its_own_reason(monkeypatch):
    apps = mock.Mock()
    apps.read_namespaced_deployment_status.return_value = _deployment(0)
    core = mock.Mock()
    core.list_namespaced_pod.return_value = SimpleNamespace(
        items=[_pod(1, waiting_reason="ImagePullBackOff")])
    monkeypatch.setattr("kubernetes.client.AppsV1Api", lambda: apps)
    monkeypatch.setattr("kubernetes.client.CoreV1Api", lambda: core)

    with pytest.raises(RuntimeError) as excinfo:
        service_deploy.wait_for_service_ready(timeout_s=0.1)

    message = str(excinfo.value)
    assert "ImagePullBackOff" in message
    # And how to look further, since the reason alone rarely ends the investigation.
    assert "kubectl" in message


def test_the_newest_pod_is_the_one_reported(monkeypatch):
    """A rollout leaves the old pod Running; its contentment is not the answer."""
    apps = mock.Mock()
    apps.read_namespaced_deployment_status.return_value = _deployment(0)
    core = mock.Mock()
    core.list_namespaced_pod.return_value = SimpleNamespace(items=[
        _pod(1, phase="Running"),
        _pod(2, waiting_reason="CreateContainerConfigError"),
    ])
    monkeypatch.setattr("kubernetes.client.AppsV1Api", lambda: apps)
    monkeypatch.setattr("kubernetes.client.CoreV1Api", lambda: core)

    with pytest.raises(RuntimeError, match="CreateContainerConfigError"):
        service_deploy.wait_for_service_ready(timeout_s=0.1)
