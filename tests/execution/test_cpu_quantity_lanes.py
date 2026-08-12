# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``execution.containers.<name>.resources.cpu`` as each lane has to spell it.

The declaration is one line in the ``.vast``, but the two lanes do not accept the same
form: Kubernetes takes a *quantity* (``500m`` is legal), Compose takes a *decimal core
count* (``cpus: '500m'`` is a validation error). A ``.vast`` that validates and runs on
the cluster must not fail on the local lane, so the conversion happens where the compose
file is written — and only there. These tests pin both spellings, because the failure
they guard against is invisible until the other lane is used.
"""

import pytest

from robovast.execution.cluster_execution.kubernetes_backend import BatchJobRunner
from robovast.execution.execution_utils.execute_local import (_compose_cpus,
                                                              _compose_resources_block)


# -- the local lane: Compose's decimal core count ------------------------------------

@pytest.mark.parametrize("declared,rendered", [
    # Whole cores stay integral. Compose accepts '4.0', but every existing campaign
    # renders '4' and a diff that changes only that is noise.
    (4, "4"),
    ("4", "4"),
    (4.0, "4"),
    # Fractional cores pass through as themselves.
    (0.5, "0.5"),
    (4.75, "4.75"),
    # Millicores are the conversion this exists for.
    ("500m", "0.5"),
    ("250m", "0.25"),
    ("1000m", "1"),
])
def test_a_cpu_declaration_reaches_compose_as_a_core_count(declared, rendered):
    assert _compose_cpus(declared) == rendered


def test_an_unparseable_cpu_is_passed_through_rather_than_dropped():
    """The config layer already rejects these. If one ever reaches here, Compose's own
    error naming the bad value beats this silently removing the limit — a run that
    quietly had no CPU ceiling is the harder failure to notice."""
    assert _compose_cpus("lots") == "lots"


def test_the_compose_resources_block_carries_the_converted_value():
    block = _compose_resources_block("500m", "2Gi")
    assert "cpus: '0.5'" in block
    # memory needs no conversion: Compose takes the same suffixed form Kubernetes does.
    assert "memory: 2Gi" in block


def test_no_resources_declared_writes_no_block():
    assert _compose_resources_block(None, None) == ""


# -- the cluster lane: a Kubernetes quantity, verbatim -------------------------------

def _manifest_for(cpu):
    """``get_job_manifest`` reads only the two attributes set here — no cluster."""
    runner = object.__new__(BatchJobRunner)
    runner.namespace = "robovast"
    runner.kube_context = None
    return runner.get_job_manifest("img:1", {"cpu": cpu, "memory": None}, [])


@pytest.mark.parametrize("cpu,quantity", [(4, "4"), (0.5, "0.5"), ("500m", "500m")])
def test_the_cluster_lane_keeps_the_declared_spelling(cpu, quantity):
    """Unlike Compose, Kubernetes parses the quantity itself, so the declaration goes in
    as written — and ``0.5`` must not be rounded to a whole core on the way."""
    container = _manifest_for(cpu)['spec']['template']['spec']['containers'][0]
    assert container['resources']['requests']['cpu'] == quantity
    assert container['resources']['limits']['cpu'] == quantity
