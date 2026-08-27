# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The deployed service's environment must stay complete when something is added to it.

**The regression this file exists for, and it reached a live cluster.** ``deploy_service``'s
``env`` parameter is the WHOLE environment, not an addition to it -- ``service_manifests``
builds the standard set only when ``env is None``. Adding the job node pool by passing a
one-element list therefore *replaced* ``ROBOVAST_CLUSTER_CONFIG_NAME``,
``ROBOVAST_CLUSTER_CONFIG_KWARGS``, ``ROBOVAST_KUBE_CONTEXT`` and ``ROBOVAST_NAMESPACE``.

Setup still reported success. The service came up, answered ``get_service_info``, and failed
every campaign and every ``exec_in_container`` with "cluster config not configured
(ROBOVAST_CLUSTER_CONFIG_NAME); the service must be deployed by 'vast exec cluster setup'" --
a message pointing at the operator's procedure rather than at the deploy that caused it.

So these tests assert the *whole* environment rather than only the variable being added.
"""

from robovast.execution.cluster_execution.node_placement import JOB_NODE_POOL_ENV
from robovast.execution.cluster_execution.service_deploy import _cluster_env

#: What an in-cluster service cannot work without. ROBOVAST_CLUSTER_CONFIG_NAME is the one
#: that failed loudly; the others fail in narrower ways and are no less required.
REQUIRED = ("ROBOVAST_NAMESPACE", "ROBOVAST_CLUSTER_CONFIG_NAME",
            "ROBOVAST_CLUSTER_CONFIG_KWARGS", "ROBOVAST_KUBE_CONTEXT")


def _env(**kwargs):
    return {e["name"]: e["value"]
            for e in _cluster_env("default", "rke2", {}, "local", **kwargs)}


def test_adding_the_node_pool_does_not_displace_the_cluster_config():
    """The exact regression: a service deployed without its config name."""
    env = _env(job_node_labels={"pool": "batch"})
    for name in REQUIRED:
        assert name in env, f"{name} was lost when the node pool was added"
    assert env["ROBOVAST_CLUSTER_CONFIG_NAME"] == "rke2"


def test_the_pool_is_written_even_when_there_is_none():
    """Empty, not absent: setup writes the cluster's configuration on every run, so omitting
    the option must CLEAR a previously configured pool rather than leave it in force."""
    env = _env()
    assert env[JOB_NODE_POOL_ENV] == ""
    for name in REQUIRED:
        assert name in env


def test_the_pool_round_trips():
    import json

    env = _env(job_node_labels={"robovast.io/node-id": "node-abc"})
    assert json.loads(env[JOB_NODE_POOL_ENV]) == {"robovast.io/node-id": "node-abc"}


def test_a_deploy_that_names_no_config_still_carries_the_rest():
    """``config_name=None`` is a legitimate call (an upgrade that only re-bakes the ingress),
    and it must not be confused with the failure above -- there the name was DROPPED."""
    env = {e["name"]: e["value"] for e in _cluster_env("default", None, {}, "local")}
    assert "ROBOVAST_CLUSTER_CONFIG_NAME" not in env
    assert env["ROBOVAST_NAMESPACE"] == "default"
    assert env["ROBOVAST_KUBE_CONTEXT"] == "local"
