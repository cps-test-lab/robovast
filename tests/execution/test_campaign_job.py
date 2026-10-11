# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""What every admitted campaign Job shares, decided in one place.

A campaign Job is a one-shot pod the admission queue places on a campaign node. Its labels,
retry policy, toleration, pull secret and pin come from
:mod:`~robovast.execution.cluster_execution.campaign_job`.
"""

from robovast.execution.cluster_execution.campaign_job import (apply_campaign_pod_policy,
                                                               campaign_job_manifest,
                                                               pin_campaign_job)
from robovast.execution.cluster_execution.node_placement import (CAMPAIGN_NODE_TOLERATIONS,
                                                                 JOB_NODE_POOL_ENV)


def _job(**kw):
    args = {"name": "j", "namespace": "ns", "jobgroup": "g", "campaign_id": "Camp_1",
            "pod_spec": {"containers": [{"name": "c"}]}, "ttl_seconds": 30}
    args.update(kw)
    return campaign_job_manifest(**args)


def test_a_campaign_job_is_one_attempt_labelled_on_job_and_pod():
    job = _job(labels={"job-kind": "calibration"})
    labels = {"jobgroup": "g", "campaign-id": "camp-1", "job-kind": "calibration"}
    assert job["metadata"]["labels"] == labels
    assert job["spec"]["template"]["metadata"]["labels"] == labels
    assert job["spec"]["backoffLimit"] == 0
    assert job["spec"]["ttlSecondsAfterFinished"] == 30
    assert job["spec"]["template"]["spec"]["restartPolicy"] == "Never"


def test_the_pod_tolerates_campaign_nodes_and_pulls_with_the_secret():
    spec = _job(pull_secret="regcred")["spec"]["template"]["spec"]
    for toleration in CAMPAIGN_NODE_TOLERATIONS:
        assert dict(toleration) in spec["tolerations"]
    assert spec["imagePullSecrets"] == [{"name": "regcred"}]


def test_no_pull_secret_leaves_the_spec_without_one():
    assert "imagePullSecrets" not in _job()["spec"]["template"]["spec"]


def test_the_policy_is_idempotent():
    spec = {}
    apply_campaign_pod_policy(spec)
    apply_campaign_pod_policy(spec)
    assert spec["tolerations"] == [dict(t) for t in CAMPAIGN_NODE_TOLERATIONS]


def test_the_pin_narrows_the_pool_and_nothing_is_touched_without_either(monkeypatch):
    monkeypatch.delenv(JOB_NODE_POOL_ENV, raising=False)
    bare = {"metadata": {}}
    assert pin_campaign_job(bare, None) == {"metadata": {}}

    monkeypatch.setenv(JOB_NODE_POOL_ENV, '{"pool": "a"}')
    pinned = pin_campaign_job(_job(), "node-1")
    selector = pinned["spec"]["template"]["spec"]["nodeSelector"]
    assert selector["pool"] == "a"
    assert "node-1" in selector.values()
