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

Twice over, because a pod that cannot start yet is indistinguishable from one that never
will -- and the batch loop answers them differently. On the scheduling side both leave
the pod ``Unschedulable`` with the same shape of message, but a run waiting behind a
neighbour's run starts on its own and a run larger than any machine never does. On the
image side both leave it ``ErrImagePull``, but a pull the kubelet or registry is
rate-limiting comes up the queue and a manifest that does not exist stays absent.

Getting either backwards costs a campaign: each of these mistakes has already ended a
50-batch search two thirds of the way through.
"""

import types

from robovast.execution.cluster_execution.cluster_execution import (
    BLOCKED_GRACE_SECONDS, CONTENDED_GRACE_SECONDS, blocked_and_contended_reasons,
    image_pull_is_throttled, list_jobs_with_phase, pod_fits_any_node,
    unschedulable_is_contention)

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


def test_an_image_verdict_is_never_contention():
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


# -- the image side of the same distinction -----------------------------------

#: What the kubelet actually wrote when two campaigns started a batch at the same second.
THROTTLED = "Back-off pulling image: pull QPS exceeded"


def test_a_rate_limited_pull_reads_as_throttled():
    assert image_pull_is_throttled(THROTTLED)
    assert image_pull_is_throttled(
        "toomanyrequests: You have reached your pull rate limit.")


def test_a_verdict_about_the_image_is_not_throttling():
    """No amount of waiting produces an image that is not there, a credential the
    registry will not accept, or a host that does not resolve."""
    assert not image_pull_is_throttled(
        'manifest for repo/img:tag not found: manifest unknown')
    assert not image_pull_is_throttled("unauthorized: authentication required")
    assert not image_pull_is_throttled(
        'dial tcp: lookup registry.example.com: no such host')
    assert not image_pull_is_throttled("")


def test_a_throttled_pull_is_reported_as_contended():
    """The failure this exists to prevent: two of thirty-five jobs rate-limited on their
    pull, failed on the sixty-second timer, and a 50-batch search ended on batch 34."""
    core = _Core([_pod("run-7", waiting=("ErrImagePull", "pull QPS exceeded"))])
    blocked, contended = blocked_and_contended_reasons(core, "ns", "sel")
    assert blocked == {"run-7": "ErrImagePull: pull QPS exceeded"}
    assert contended == blocked


def test_a_throttled_pull_needs_no_node_list():
    """Unlike a reservation, a throttled pull has no node fact that could make it
    permanent -- so an unreadable node list must not downgrade it to unrecoverable."""
    core = _Core([_pod("run-7", waiting=("ErrImagePull", THROTTLED))],
                 node_error=RuntimeError("nodes forbidden"))
    _, contended = blocked_and_contended_reasons(core, "ns", "sel")
    assert contended == {"run-7": f"ErrImagePull: {THROTTLED}"}


def test_a_throttled_pull_beside_a_broken_one_still_leaves_work_to_fail():
    """`contended` is a subset, never a verdict on the batch: the batch loop fails on the
    short timer while ANY blocked job is missing from it."""
    core = _Core([_pod("run-7", waiting=("ErrImagePull", THROTTLED)),
                  _pod("run-8", waiting=("InvalidImageName", "could not parse reference"))])
    blocked, contended = blocked_and_contended_reasons(core, "ns", "sel")
    assert set(blocked) == {"run-7", "run-8"}
    assert set(blocked) - set(contended) == {"run-8"}


def test_only_a_pull_reason_can_be_throttled():
    """A container config the kubelet rejects never involved a registry, so a message
    that happens to contain the vocabulary must not buy it the long grace."""
    core = _Core([_pod("run-7", waiting=("CreateContainerConfigError",
                                         "secret rate limit not found"))])
    blocked, contended = blocked_and_contended_reasons(core, "ns", "sel")
    assert blocked and not contended


# -- what the reader is shown --------------------------------------------------
#
# The batch loop's distinction above is about how long to wait. This one is about what a
# human sees while it waits, and it is a separate mistake to get wrong: a `blocked` row is
# red and its count is the one that asks somebody to intervene, so spending either on a
# job that starts by itself teaches the reader to ignore both.


def _job(name, *, succeeded=0, active=0, failed=0, suspend=False):
    return types.SimpleNamespace(
        metadata=types.SimpleNamespace(name=name, labels={}, annotations={}),
        spec=types.SimpleNamespace(suspend=suspend),
        status=types.SimpleNamespace(succeeded=succeeded, active=active, failed=failed))


class _Batch:
    def __init__(self, jobs):
        self._jobs = jobs

    def list_namespaced_job(self, namespace, label_selector):
        return types.SimpleNamespace(items=self._jobs)


def _listed(batch, core):
    return {j.metadata.name: (phase, detail)
            for j, phase, detail in list_jobs_with_phase(batch, core, "ns", "sel")}


def test_a_contended_job_is_listed_pending_with_the_schedulers_reason():
    """`pending` is the literal truth about it -- the pod exists and has not started --
    and the message stays, because "why is this one not moving" is a fair question even
    when the answer is "it will"."""
    listed = _listed(_Batch([_job("run-7", active=1)]), _Core([_pod("run-7")]))
    assert listed["run-7"] == ("pending", f"Unschedulable: {BUSY}")


def test_a_throttled_pull_is_listed_pending_too():
    core = _Core([_pod("run-7", waiting=("ErrImagePull", THROTTLED))])
    listed = _listed(_Batch([_job("run-7", active=1)]), core)
    assert listed["run-7"] == ("pending", f"ErrImagePull: {THROTTLED}")


def test_a_job_that_will_not_start_is_still_listed_blocked():
    """The counterpart that must not move: a reservation no machine can satisfy looks
    identical to the scheduler and is the reason the red row exists."""
    listed = _listed(_Batch([_job("run-7", active=1)]), _Core([_pod("run-7", cpu="200")]))
    assert listed["run-7"] == ("blocked", f"Unschedulable: {BUSY}")


def test_unreadable_nodes_still_list_as_blocked():
    """Same strictness as the batch loop: with no node sizes an impossible request cannot
    be ruled out, so the listing must not soften it either."""
    core = _Core([_pod("run-7")], node_error=RuntimeError("nodes forbidden"))
    listed = _listed(_Batch([_job("run-7", active=1)]), core)
    assert listed["run-7"][0] == "blocked"


def test_a_contended_job_keeps_its_reason_over_kueues():
    """A suspended Job has no pod and so cannot be contended, which makes this state
    unreachable against a real cluster. It is pinned because the `waiting` branch assigns
    `detail` unconditionally and is now reachable by a job that already has one: without
    its guard, the scheduler's message would be replaced by Kueue's."""
    job = _job("run-7", active=1, suspend=True)
    listed = _listed(_Batch([job]), _Core([_pod("run-7")]))
    assert listed["run-7"] == ("pending", f"Unschedulable: {BUSY}")
