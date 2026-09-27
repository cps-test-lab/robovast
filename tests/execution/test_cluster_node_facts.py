# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The cluster backend answers ``node_facts`` from the cluster's own nodes.

A run's record names its machine by a hashed label; the facts about that machine (capacity,
allocatable, OS) come from the node list, read once by the backend. A backend that could not
read it records every run with no machine facts at all.
"""

import types

from robovast.common.execution import node_label
from robovast.execution.cluster_execution import kube_client
from robovast.execution.cluster_execution.kubernetes_backend import KubernetesBackend


def test_node_facts_are_read_from_the_cluster(monkeypatch):
    node = types.SimpleNamespace(
        metadata=types.SimpleNamespace(name="node-a", labels={"kubernetes.io/hostname": "node-a",
                                                              "zone": "z1"}),
        status=types.SimpleNamespace(capacity={"cpu": "8"}, allocatable={"cpu": "7"},
                                     node_info=None))
    asked = []

    def _core(context=None):
        asked.append(context)
        return types.SimpleNamespace(list_node=lambda: types.SimpleNamespace(items=[node]))

    monkeypatch.setattr(kube_client, "core_v1_client", _core)
    backend = KubernetesBackend(cluster_config=object(), kube_context="ctx")

    facts = backend.node_facts(node_label("node-a"))

    assert facts == {"capacity": {"cpu": "8"}, "allocatable": {"cpu": "7"}, "node_info": {},
                     "labels": {"zone": "z1"}}
    assert asked == ["ctx"]
