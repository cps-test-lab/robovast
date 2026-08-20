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

"""Telling a busy cluster from a broken one.

Both leave a pod ``Unschedulable`` with the same shape of message, and the batch loop
answers them differently: a run waiting behind a neighbour's run starts on its own, a
run larger than any machine never does. Getting this backwards costs a campaign.
"""

import types

from robovast.execution.cluster_execution.cluster_execution import (
    BLOCKED_GRACE_SECONDS, CONTENDED_GRACE_SECONDS, blocked_and_contended_reasons,
    pod_fits_any_node, unschedulable_is_contention)

#: What the scheduler actually wrote when two campaigns filled the node.
BUSY = ("0/1 nodes are available: 1 Insufficient cpu. no new claims to deallocate, "
        "preemption: 0/1 nodes are available: 1 No preemption victims found for "
        "incoming pod.")


def _container(cpu=None, memory=None):
    requests = {}
    if cpu is not None:
        requests["cpu"] = cpu
    if memory is not None:
        requests["memory"] = memory
    return types.SimpleNamespace(
        name="scenario", resources=types.SimpleNamespace(requests=requests))


def _pod(job_name, message=BUSY, *, cpu="8", memory="8Gi", waiting=None):
    """A Pending pod for *job_name*, unschedulable with *message* by default."""
    conditions = None
    container_statuses = None
    if waiting is not None:
        container_statuses = [types.SimpleNamespace(
            state=types.SimpleNamespace(
                waiting=types.SimpleNamespace(reason=waiting[0], message=waiting[1])))]
    else:
        conditions = [types.SimpleNamespace(type="PodScheduled", status="False",
                                            reason="Unschedulable", message=message)]
    return types.SimpleNamespace(
        metadata=types.SimpleNamespace(
            name=f"{job_name}-pod", namespace="ns",
            labels={"batch.kubernetes.io/job-name": job_name}),
        spec=types.SimpleNamespace(containers=[_container(cpu, memory)],
                                   init_containers=None),
        status=types.SimpleNamespace(
            phase="Pending", container_statuses=container_statuses,
            init_container_statuses=None, reason=None, message=None,
            conditions=conditions))


def _node(cpu="96", memory="125Gi"):
    return types.SimpleNamespace(
        metadata=types.SimpleNamespace(name="node-1"),
        status=types.SimpleNamespace(allocatable={"cpu": cpu, "memory": memory}))


class _Core:
    def __init__(self, pods, nodes=None, node_error=None):
        self._pods = pods
        self._nodes = nodes if nodes is not None else [_node()]
        self._node_error = node_error

    def list_namespaced_pod(self, namespace, label_selector):
        return types.SimpleNamespace(items=self._pods)

    def list_node(self):
        if self._node_error is not None:
            raise self._node_error
        return types.SimpleNamespace(items=self._nodes)


# -- the message alone --------------------------------------------------------

def test_insufficient_resource_reads_as_contention():
    assert unschedulable_is_contention(BUSY)
    assert unschedulable_is_contention(
        "0/3 nodes are available: 2 Insufficient cpu, 1 Insufficient memory.")


def test_a_cause_that_waiting_cannot_fix_is_not_contention():
    """A taint or an unmatched selector describes a cluster that looks the same in an
    hour, so it must keep the short grace."""
    assert not unschedulable_is_contention(
        "0/1 nodes are available: 1 node(s) had untolerated taint {dedicated: batch}.")
    assert not unschedulable_is_contention(
        "0/2 nodes are available: 2 node(s) didn't match Pod's node affinity/selector.")


def test_one_permanent_cause_among_transient_ones_is_not_contention():
    """Mixed causes get the strict answer: some node is refusing for good."""
    assert not unschedulable_is_contention(
        "0/3 nodes are available: 1 Insufficient cpu, 2 node(s) had untolerated taint "
        "{dedicated: batch}.")


def test_a_message_with_no_stated_cause_is_not_contention():
    assert not unschedulable_is_contention("0/1 nodes are available")
    assert not unschedulable_is_contention("")


# -- the message plus the machines --------------------------------------------

def test_a_reservation_larger_than_every_node_does_not_fit():
    """"Insufficient cpu" is also what a run too big to ever place looks like."""
    assert not pod_fits_any_node(_pod("huge", cpu="200"), [_node(cpu="96")])
    assert pod_fits_any_node(_pod("normal", cpu="8"), [_node(cpu="96")])


def test_a_resource_no_node_advertises_does_not_fit():
    """A GPU whose device plugin is down is absence, not contention."""
    pod = _pod("gpu-job")
    pod.spec.containers[0].resources.requests["nvidia.com/gpu"] = "1"
    assert not pod_fits_any_node(pod, [_node()])


# -- what the batch loop is handed ---------------------------------------------

def test_a_run_waiting_behind_a_neighbour_is_reported_as_contended():
    core = _Core([_pod("run-7")])
    blocked, contended = blocked_and_contended_reasons(core, "ns", "sel")
    assert blocked == {"run-7": f"Unschedulable: {BUSY}"}
    assert contended == blocked


def test_a_run_too_big_for_the_cluster_is_blocked_but_not_contended():
    core = _Core([_pod("run-7", cpu="200")])
    blocked, contended = blocked_and_contended_reasons(core, "ns", "sel")
    assert blocked and not contended


def test_an_image_failure_is_never_contention():
    core = _Core([_pod("run-7", waiting=("ImagePullBackOff", "no such image"))])
    blocked, contended = blocked_and_contended_reasons(core, "ns", "sel")
    assert blocked == {"run-7": "ImagePullBackOff: no such image"}
    assert not contended


def test_unreadable_nodes_give_the_strict_answer():
    """Without node sizes there is no way to rule out an impossible request, and a
    campaign must not sit for the long grace on a guess."""
    core = _Core([_pod("run-7")], node_error=RuntimeError("nodes forbidden"))
    blocked, contended = blocked_and_contended_reasons(core, "ns", "sel")
    assert blocked and not contended


def test_contention_is_given_more_room_than_a_registry_blip():
    assert CONTENDED_GRACE_SECONDS > BLOCKED_GRACE_SECONDS
