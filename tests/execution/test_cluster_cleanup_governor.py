# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""What `vast cluster cleanup` takes away.

Kept out of ``test_cpu_governor_daemonset.py`` because that module fakes
``kubernetes.client.exceptions`` for every test, and this one drives the real teardown path.
"""

from robovast.execution.cluster_execution.node_governor import REMOVED


def test_cluster_cleanup_removes_the_governor_daemonset(monkeypatch):
    """The observed bug: `cluster cleanup` + `setup` left governor pods days old, because
    only `setup` ever removed the DaemonSet."""
    import types as _types

    from robovast.execution.cluster_execution import cluster_setup

    calls = []
    monkeypatch.setattr(cluster_setup, "get_cluster_config",
                        lambda name: _types.SimpleNamespace(
                            cleanup_cluster=lambda **kw: calls.append("cluster_config")))
    monkeypatch.setattr(cluster_setup, "delete_controller_rbac", lambda **kw: None)
    monkeypatch.setattr(cluster_setup, "uninstall_nvidia_device_plugin", lambda **kw: None)
    monkeypatch.setattr("robovast.execution.cluster_execution.cluster_execution."
                        "cleanup_cluster_campaign", lambda **kw: None)
    monkeypatch.setattr("robovast.execution.cluster_execution.image_warm."
                        "delete_warm_daemonset", lambda ns, ctx: calls.append("warm"))
    monkeypatch.setattr("robovast.execution.cluster_execution.buildkitd_deploy."
                        "delete_buildkitd", lambda ns, ctx: calls.append("buildkitd"))
    monkeypatch.setattr("robovast.execution.cluster_execution.service_deploy."
                        "delete_service", lambda **kw: calls.append("service"))
    monkeypatch.setattr("robovast.execution.cluster_execution.node_governor."
                        "delete_cpu_governor",
                        lambda ns, ctx=None: calls.append(f"governor:{ns}") or REMOVED)

    result = cluster_setup.delete_server(config_name="cfg", namespace="ns1")

    assert "governor:ns1" in calls
    # Before the service and the cluster config's own teardown: a privileged pod on every
    # node must not outlive the deployment it belongs to.
    assert calls.index("governor:ns1") < calls.index("service")
    assert result["cpu_governor"] == REMOVED
