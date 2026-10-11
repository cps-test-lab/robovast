# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The cluster starts no Job while the disk its campaigns land on is below its reserve.

On a node-directory deployment that disk is the data node's own, and the kubelet evicts every
pod there -- the service with every campaign it drives -- once free space falls below its
threshold. A campaign accepted above the reserve would otherwise go on creating Jobs, each
writing its results into that disk. So the admission queue holds everything while the disk is
short, says why, and resumes by itself once space is freed; Jobs already running go on.
"""

import logging
import types

from robovast.execution.cluster_execution.kubernetes_backend import BatchJobRunner
from robovast.execution.cluster_execution.node_admission import (CREATED, DISK_WAIT, PLANNED,
                                                                 AdmissionController, Budget,
                                                                 Capacity, JobSizing,
                                                                 NodeBudget)

_SHORT = "the results volume has 90 GB free, below the 150 GB reserve (ROBOVAST_DISK_RESERVE_GB)."


class _Provider:
    """One roomy node: nothing but the disk ever holds a Job back here."""

    def budget(self):
        return Budget(nodes=(NodeBudget(node_id="node-a", free_cpu=32.0,
                                        free_memory=128 * 1024 ** 3, free_gpu=0),),
                      counted_jobs=frozenset(), growable=False)

    def capacities(self):
        return [Capacity(node_id="node-a", cpu=32.0, memory=128 * 1024 ** 3, gpu=0)]


class _Gate:
    """A disk whose shortfall the test moves."""

    def __init__(self, short=None):
        self.short = short
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if isinstance(self.short, Exception):
            raise self.short
        return self.short


def _queue(gate):
    admission = AdmissionController(_Provider(), budget_ttl=0.0, space_gate=gate)
    created = []
    admission.submit("camp-1", [(f"job-{i}", JobSizing(cpu=1.0, memory=1024 ** 3),
                                 lambda node, i=i: created.append(i)) for i in range(3)],
                     started_at=0.0)
    return admission, created


def test_nothing_is_admitted_while_the_disk_is_below_its_reserve():
    admission, created = _queue(_Gate(_SHORT))
    assert admission.drain() == 0
    assert created == []
    assert set(admission.states("camp-1").values()) == {PLANNED}
    assert admission.refusal("camp-1") == f"{DISK_WAIT}{_SHORT}"
    assert admission.space_shortfall() == _SHORT


def test_admission_resumes_by_itself_once_space_is_freed():
    gate = _Gate(_SHORT)
    admission, created = _queue(gate)
    admission.drain()
    gate.short = None
    assert admission.drain() == 3
    assert sorted(created) == [0, 1, 2]
    assert set(admission.states("camp-1").values()) == {CREATED}
    assert admission.space_shortfall() is None


def test_a_disk_that_could_not_be_measured_holds_nothing(caplog):
    """Unmeasured is not full: holding every campaign on a reading it never needed would make
    a launch depend on it. Said once, not on every drain."""
    gate = _Gate(OSError("results root is not mounted"))
    admission, created = _queue(gate)
    with caplog.at_level(logging.WARNING):
        admission.drain()
        admission.drain()
    assert sorted(created) == [0, 1, 2]
    assert caplog.text.count("could not be measured") == 1


def test_no_gate_is_the_queue_as_it_was():
    admission = AdmissionController(_Provider(), budget_ttl=0.0)
    created = []
    admission.submit("camp-1", [("job-0", JobSizing(cpu=1.0, memory=1024 ** 3),
                                 lambda node: created.append(0))], started_at=0.0)
    assert admission.drain() == 1 and created == [0]


def test_the_campaigns_status_carries_the_wait_and_drops_it_after():
    """The log has the refusal already; the stage is what someone watching reads."""
    stages = []
    state = types.SimpleNamespace(update=lambda **kw: stages.append(kw["stage"]))
    runner = BatchJobRunner.__new__(BatchJobRunner)
    runner._state, runner._batch_tag = state, "batch-1"
    runner._publish_space_wait(_SHORT)
    runner._publish_space_wait(_SHORT)      # unchanged: not written again
    runner._publish_space_wait(None)
    assert stages == [f"{DISK_WAIT}{_SHORT}", None]
