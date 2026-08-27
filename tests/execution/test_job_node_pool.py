# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The campaign job node pool -- ``vast exec cluster setup --jobs-node-label KEY=VALUE``.

The setting's only implementation used to be Kueue's ResourceFlavor nodeLabels, reached
through a ``.vast``; for one release after Kueue was retired it was refused outright. It is
now a property of the CLUSTER, carried to the service in ``ROBOVAST_JOB_NODE_LABELS``, and it
means what ``docs/configuration.rst`` always claimed: a ``nodeSelector`` on each job pod,
backed by an admission controller that counts capacity only inside the same pool.
"""

import types

import pytest

from robovast.execution.cluster_execution import kubernetes_backend as kb
from robovast.execution.cluster_execution.node_placement import (JOB_NODE_POOL_ENV,
                                                                 NODE_ID_LABEL,
                                                                 job_node_pool)


def _manifest(existing=None):
    spec = {}
    if existing:
        spec["nodeSelector"] = dict(existing)
    return {"spec": {"template": {"spec": spec}}}


def _pin(monkeypatch, pool, node_id, existing=None):
    if pool is None:
        monkeypatch.delenv(JOB_NODE_POOL_ENV, raising=False)
    else:
        monkeypatch.setenv(JOB_NODE_POOL_ENV, pool)
    m = kb.BatchJobRunner._pin(kb.BatchJobRunner(), _manifest(existing), node_id)
    return m["spec"]["template"]["spec"].get("nodeSelector")


# -- the contract ------------------------------------------------------------------------

def test_the_pool_reaches_the_pod(monkeypatch):
    """Not just the accounting. The provider counts only nodes in the pool, so a pod free to
    land outside it would run on capacity nothing reserved."""
    assert _pin(monkeypatch, '{"node-pool": "primary"}', None) == {"node-pool": "primary"}


def test_the_per_run_pin_narrows_the_pool_rather_than_replacing_it(monkeypatch):
    """A selector that dropped the pool would defeat the confinement it was placed inside."""
    got = _pin(monkeypatch, '{"node-pool": "primary"}', "node-abc")
    assert got == {"node-pool": "primary", NODE_ID_LABEL: "node-abc"}


def test_what_the_spec_already_carried_survives(monkeypatch):
    got = _pin(monkeypatch, '{"node-pool": "primary"}', "node-abc", existing={"zone": "a"})
    assert got == {"zone": "a", "node-pool": "primary", NODE_ID_LABEL: "node-abc"}


def test_no_pool_configured_is_every_node(monkeypatch):
    assert _pin(monkeypatch, None, None) is None
    assert _pin(monkeypatch, None, "node-abc") == {NODE_ID_LABEL: "node-abc"}


def test_an_empty_value_clears_a_previous_pool(monkeypatch):
    """Setup writes the var on every run, empty included. Without that, deleting the setting
    from the .vast would leave the old pool in force and the operator's file would stop
    describing the cluster it configures."""
    assert _pin(monkeypatch, "", None) is None


# -- fail loudly rather than meaning "every node" -----------------------------------------

@pytest.mark.parametrize("raw", ['{"a": 1}', "[]", "not json", '"a"'])
def test_a_value_that_cannot_be_read_raises(monkeypatch, raw):
    """A typo that silently became "every node" would scatter a campaign across machines the
    operator excluded, and the symptom shows up nowhere near the cause."""
    monkeypatch.setenv(JOB_NODE_POOL_ENV, raw)
    with pytest.raises(ValueError, match=JOB_NODE_POOL_ENV):
        job_node_pool()
