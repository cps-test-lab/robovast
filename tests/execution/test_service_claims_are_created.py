# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A class-backed workspaces store must actually get its claims.

``service_manifests`` has always rendered a PersistentVolumeClaim for the workspaces and one
for the campaign results, and the tests beside this one assert their shape. Nothing created
them: ``deploy_service`` dispatches its manifests by kind and PersistentVolumeClaim was not
among the kinds it handled, so the claims were rendered, asserted, and dropped. A deployment
given a StorageClass got a Deployment mounting volumes the cluster had never been asked for,
and its pod sat Pending against them.
"""

from unittest.mock import MagicMock

import pytest

from robovast.execution.cluster_execution import service_deploy


@pytest.fixture
def apis(monkeypatch):
    """Every API ``deploy_service`` reaches for, recording the order it calls them in."""
    from kubernetes import client as kclient

    order = []

    core, rbac, apps, net = (MagicMock(name=n) for n in ("core", "rbac", "apps", "net"))

    def _record(api, method):
        # side_effect, not a replacement: the mock keeps recording its own calls, so the
        # test can assert both what was asked for and the order it was asked in.
        getattr(api, method).side_effect = lambda *a, **k: order.append(method)

    _record(core, "create_namespaced_persistent_volume_claim")
    _record(apps, "create_namespaced_deployment")

    monkeypatch.setattr(kclient, "CoreV1Api", lambda *a, **k: core)
    monkeypatch.setattr(kclient, "RbacAuthorizationV1Api", lambda *a, **k: rbac)
    monkeypatch.setattr(kclient, "AppsV1Api", lambda *a, **k: apps)
    monkeypatch.setattr(kclient, "NetworkingV1Api", lambda *a, **k: net)
    # Both spellings: `deploy_service` resolves its own loader through a deferred
    # `from .kube_client import load_kube_config`, so patching service_deploy's alias alone
    # leaves that one reaching for a real kubeconfig -- which passes on a developer's machine
    # and fails on a runner that has none.
    from robovast.execution.cluster_execution import kube_client

    monkeypatch.setattr(kube_client, "load_kube_config", lambda *a, **k: None)
    monkeypatch.setattr(service_deploy, "_load_kube_config", lambda *a, **k: None)
    monkeypatch.setattr(service_deploy, "service_storage_from_cluster", lambda *a, **k: {})
    monkeypatch.setattr(service_deploy, "_resolve_data_node", lambda *a, **k: {})
    monkeypatch.setattr(service_deploy, "existing_auth_token", lambda *a, **k: "token")
    monkeypatch.setattr(service_deploy, "existing_index_password", lambda *a, **k: "pw")
    return core, apps, order


def test_a_workspaces_class_creates_its_claims_before_the_deployment(apis):
    """A pod scheduled against a claim that does not exist yet stays Pending."""
    core, _apps, order = apis
    service_deploy.deploy_service(namespace="default", config_name="rke2",
                                  workspaces_storage_class="fast-ssd")

    claimed = [c.args[1]["metadata"]["name"]
               for c in core.create_namespaced_persistent_volume_claim.call_args_list]
    assert set(claimed) == {service_deploy.WORKSPACES_VOLUME_NAME,
                            service_deploy.RESULTS_VOLUME_NAME}
    assert order.index("create_namespaced_persistent_volume_claim") < \
        order.index("create_namespaced_deployment")


def test_a_hostpath_deployment_asks_for_no_claims(apis):
    """The default backing needs no volume, and asking for one would leave it Pending."""
    core, _apps, _order = apis
    service_deploy.deploy_service(namespace="default", config_name="rke2")
    core.create_namespaced_persistent_volume_claim.assert_not_called()
