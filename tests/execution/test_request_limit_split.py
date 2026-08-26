# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Splitting a container's reservation from its ceiling.

The reservation is what the cluster packs by and what RoboVAST's own admission measures a
job with; the limit only decides when the kernel throttles. Confusing the two either
over-admits (packing by a ceiling nothing reserves) or throttles a container that reserved
room it is then not allowed to use -- so the pair is pinned here rather than left to the two
call sites that stamp it.
"""

import pytest

from robovast.common.config import ResourcesConfig
from robovast.execution.cluster_execution.kubernetes_backend import stamp_resources


def _stamped(**resources):
    spec = {}
    stamp_resources(spec, resources)
    return spec["resources"]


def test_without_a_limit_the_two_are_equal_as_they_always_were():
    """The default, and every campaign written before these fields existed. Byte-identical to
    what those manifests carried, so no existing campaign's pod shape moves."""
    res = _stamped(cpu=3, memory="640Mi")
    assert res["requests"] == {"cpu": "3", "memory": "640Mi"}
    assert res["limits"] == {"cpu": "3", "memory": "640Mi"}


def test_a_declared_limit_raises_only_the_ceiling():
    """The simulator's shape: reserve the sustained figure, burst into whatever is idle."""
    res = _stamped(cpu=0.5, cpu_limit=6, memory="512Mi", memory_limit="4Gi")
    assert res["requests"] == {"cpu": "0.5", "memory": "512Mi"}
    assert res["limits"] == {"cpu": "6", "memory": "4Gi"}


def test_neither_side_is_ever_left_empty():
    """``JOB_TEMPLATE`` reads AVAILABLE_CPUS/AVAILABLE_MEM from ``resourceFieldRef:
    limits.*``, and the downward API substitutes the NODE's allocatable for an unset limit --
    so a scenario would size itself to the whole machine, which is a wrong answer that looks
    like a right one."""
    res = _stamped(cpu=1)
    assert res["limits"].get("cpu"), "an unset limit makes the pod read the node's capacity"
    res = _stamped(memory="1Gi")
    assert res["limits"].get("memory")


def test_admission_sizes_a_job_by_the_request_not_the_ceiling():
    """The invariant the split could break, and the expensive direction to get wrong.

    Admission counts what the scheduler counts. If it read the ceiling instead, a simulator
    reserving 0.5 and bursting to 6 would be charged 6 -- so a cluster that can hold forty
    such pods would admit six, and the split meant to buy concurrency would cost it.
    """
    from robovast.execution.cluster_execution import kubernetes_backend as kb

    r = kb.BatchJobRunner()
    r.manifest = {}
    r.create_job_manifest = lambda job, total, node_figures=None: {"spec": {"template": {"spec": {
        "containers": [{"resources": {"requests": {"cpu": "3", "memory": "640Mi"},
                                      "limits": {"cpu": "3", "memory": "640Mi"}}}],
        "initContainers": [
            {"restartPolicy": "Always",
             "resources": {"requests": {"cpu": "0.5", "memory": "512Mi"},
                           "limits": {"cpu": "6", "memory": "4Gi"}}}]}}}}

    sizing = r._job_sizing(object(), 1)
    assert sizing.cpu == pytest.approx(3.5), "admission must pack by the reservation"
    assert sizing.cpu != pytest.approx(9.0), "sizing from the ceiling would admit 6 of 40"


def test_a_ceiling_below_its_own_reservation_is_refused_in_the_config():
    """Kubernetes rejects such a pod outright, so the campaign would die at submission with
    an API message several layers from the two lines that disagree."""
    with pytest.raises(Exception, match="below"):
        ResourcesConfig(cpu=4, cpu_limit=1)
    with pytest.raises(Exception, match="below"):
        ResourcesConfig(memory="4Gi", memory_limit="512Mi")
    # Equal is the normal case, not an error.
    assert ResourcesConfig(cpu=2, cpu_limit=2).cpu_limit == 2


def test_a_per_cluster_list_is_not_compared_here():
    """Resolved per context much later; guessing which entry pairs with which would report a
    conflict the active cluster may not have."""
    assert ResourcesConfig(cpu=[{"ctx-a": 4}], cpu_limit=1).cpu_limit == 1


def test_an_unknown_resource_key_is_refused_rather_than_ignored():
    """The failure mode this whole pair could have shipped with.

    Pydantic ignores unknown keys by default, so a deployment predating a field drops it in
    SILENCE and runs a different allocation than the file asks for. That happened here:
    ``cpu_limit`` landed while the deployed service still lacked it, and the effect would
    have been a simulator capped at 0.5 -- BELOW the 0.75 it had been running at -- with no
    error and nothing in a log to say so. A typo has the identical shape and is likelier.
    """
    with pytest.raises(Exception, match="[Ee]xtra"):
        ResourcesConfig(cpu=1, cpu_limits=6)
    with pytest.raises(Exception, match="[Ee]xtra"):
        ResourcesConfig(cpu=1, memroy="1Gi")
