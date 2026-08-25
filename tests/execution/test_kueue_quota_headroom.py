# Copyright (C) 2026 Frederik Pasch
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions
# and limitations under the License.
#
# SPDX-License-Identifier: Apache-2.0

"""The ClusterQueue may only promise capacity the scheduler can actually hand out.

Quota sized at 100% of allocatable admits one job more than the nodes hold whenever
anything else runs on them -- and something always does, starting with Kueue's own
controller. The job that loses the race sits ``Unschedulable`` and the batch fails.
"""

import types
from unittest import mock

import yaml

from robovast.execution.cluster_execution import kubernetes_kueue as kk

GI = 1024 ** 3


def _node(cpu="96", memory=f"{128 * GI}", *, capacity_cpu=None):
    """A node advertising *cpu*/*memory* as allocatable. ``capacity_cpu`` defaults to
    more than that, i.e. a kubelet that does reserve something for the system."""
    return types.SimpleNamespace(
        metadata=types.SimpleNamespace(name="node-1"),
        status=types.SimpleNamespace(
            allocatable={"cpu": cpu, "memory": memory},
            capacity={"cpu": capacity_cpu or f"{_parse(cpu) + 2}",
                      "memory": memory}))


def _parse(value):
    return kk._parse_resource(value)


def _pod(name, cpu, memory, *, job=None, node="node-1", sidecar=None):
    """A pod on *node*; ``job`` names the Job that owns it, ``sidecar`` adds a native
    sidecar's ``(cpu, memory)`` — which Kubernetes adds to the pod's effective total."""
    labels = {"batch.kubernetes.io/job-name": job} if job else {}
    init = None
    if sidecar is not None:
        init = [types.SimpleNamespace(
            name="sim", restart_policy="Always",
            resources=types.SimpleNamespace(
                requests={"cpu": sidecar[0], "memory": sidecar[1]}))]
    return types.SimpleNamespace(
        metadata=types.SimpleNamespace(name=name, namespace="default", labels=labels),
        spec=types.SimpleNamespace(
            node_name=node, init_containers=init,
            containers=[types.SimpleNamespace(
                name="main",
                resources=types.SimpleNamespace(
                    requests={"cpu": cpu, "memory": memory}))]))


def _queued_job(name, namespace="default"):
    return types.SimpleNamespace(
        metadata=types.SimpleNamespace(name=name, namespace=namespace))


def _sized(pods, jobs=(), job_error=None, node=None):
    """Run the quota computation against one 96-core / 128Gi node holding *pods*."""
    with mock.patch("robovast.execution.cluster_execution.kube_client.load_kube_config"), \
         mock.patch.object(kk.client, "CoreV1Api") as core, \
         mock.patch.object(kk.client, "BatchV1Api") as batch:
        core.return_value.list_node.return_value = types.SimpleNamespace(
            items=[node or _node()])
        core.return_value.list_pod_for_all_namespaces.return_value = \
            types.SimpleNamespace(items=list(pods))
        if job_error is not None:
            batch.return_value.list_job_for_all_namespaces.side_effect = job_error
        else:
            batch.return_value.list_job_for_all_namespaces.return_value = \
                types.SimpleNamespace(items=list(jobs))
        return kk.get_cluster_allocatable_resources()


def test_an_empty_cluster_still_offers_everything():
    assert _sized([]) == (96, "128Gi")


def test_infrastructure_reservations_come_off_the_quota():
    """Kueue's controller, the CNI, MinIO, the service: the scheduler has already
    subtracted these from the node and no Workload accounts for them."""
    assert _sized([_pod("kueue-controller", "500m", f"{4 * GI}"),
                   _pod("minio", "1", f"{2 * GI}")]) == (94, "122Gi")


def test_campaign_pods_are_left_to_kueue():
    """Subtracting an admitted campaign pod would shrink the quota to fit whatever
    happened to be running at setup time, permanently."""
    pods = [_pod("run-1", "4", f"{8 * GI}", job="run-1", sidecar=("4", f"{8 * GI}")),
            _pod("kueue-controller", "500m", f"{4 * GI}")]
    assert _sized(pods, jobs=[_queued_job("run-1")]) == (95, "124Gi")


def test_a_job_outside_the_queue_is_counted():
    """An image build is a Job too, and it is not Kueue's."""
    pods = [_pod("imgbuild", "2", f"{4 * GI}", job="imgbuild")]
    assert _sized(pods, jobs=[]) == (94, "124Gi")


def test_an_unreadable_job_list_errs_towards_a_smaller_quota():
    """Without the Job list, every Job-owned pod is assumed to be Kueue's. Too small a
    quota only makes runs queue; too large is the over-admission this prevents."""
    pods = [_pod("imgbuild", "2", f"{4 * GI}", job="imgbuild"),
            _pod("minio", "1", f"{2 * GI}")]
    assert _sized(pods, job_error=RuntimeError("forbidden")) == (95, "126Gi")


def test_pods_on_other_nodes_do_not_count():
    assert _sized([_pod("elsewhere", "8", f"{16 * GI}", node="node-2")]) == (96, "128Gi")


def test_the_quota_never_goes_to_zero():
    assert _sized([_pod("hog", "200", f"{500 * GI}")]) == (1, "1Gi")


def test_the_kueue_controller_does_not_reserve_a_campaigns_worth_of_node():
    """Its requests are a permanent hole in the capacity above; its limits are not.
    Four cores reserved for a controller was most of the headroom on a one-node lane."""
    values = yaml.safe_load(kk.KUEUE_HELM_VALUES)
    resources = values["controllerManager"]["manager"]["resources"]
    assert kk._parse_resource(resources["requests"]["cpu"]) <= 1
    assert (kk._parse_resource(resources["limits"]["cpu"])
            > kk._parse_resource(resources["requests"]["cpu"]))


def test_a_node_that_reserves_nothing_for_itself_is_called_out(caplog):
    """allocatable == capacity means the OS and the kubelet compete with pods for the
    last core. Nothing RoboVAST runs can fix that, so it says so once, at setup."""
    with caplog.at_level("WARNING"):
        assert _sized([], node=_node(cpu="96", capacity_cpu="96")) == (96, "128Gi")
    assert "reserve nothing for the system" in caplog.text


def test_a_node_with_a_kubelet_reservation_is_not_called_out(caplog):
    with caplog.at_level("WARNING"):
        _sized([], node=_node(cpu="94", capacity_cpu="96"))
    assert "reserve nothing" not in caplog.text
