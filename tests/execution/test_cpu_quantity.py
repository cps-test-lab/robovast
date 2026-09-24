# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``execution.containers.<name>.resources.cpu`` as the Job manifest spells it.

Kubernetes takes a *quantity* (``500m`` is legal), so the declaration goes into the Job
manifest as written rather than converted to a core count on the way.
"""

import pytest

from robovast.execution.cluster_execution.kubernetes_backend import BatchJobRunner
# -- a Kubernetes quantity, verbatim ------------------------------------------------

def _manifest_for(cpu):
    """``get_job_manifest`` reads only the attributes set here — no cluster."""
    runner = object.__new__(BatchJobRunner)
    runner.namespace = "robovast"
    runner.kube_context = None
    # Concurrent campaigns are ordered by start time, which is derived from the
    # timestamp in the campaign id.
    runner.campaign = "camp-2026-07-17-120000"
    return runner.get_job_manifest("img:1", {"cpu": cpu, "memory": None}, [])


@pytest.mark.parametrize("cpu,quantity", [(4, "4"), (0.5, "0.5"), ("500m", "500m")])
def test_the_job_manifest_keeps_the_declared_spelling(cpu, quantity):
    """Kubernetes parses the quantity itself, so the declaration goes in as written — and
    ``0.5`` must not be rounded to a whole core on the way."""
    container = _manifest_for(cpu)['spec']['template']['spec']['containers'][0]
    assert container['resources']['requests']['cpu'] == quantity
    assert container['resources']['limits']['cpu'] == quantity
