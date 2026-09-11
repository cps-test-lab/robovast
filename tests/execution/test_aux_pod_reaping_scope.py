# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Who may reap a campaign's auxiliary-container pod.

An aux pod has a live owner in the ordinary case: the ``AuxPodSession`` that created it
deletes it when its composition span ends. A *reaper* -- ``vast cluster jobs-cleanup``,
a campaign being deleted -- has to collect one whose span is gone. A **stop** is neither:
its driver is still composing against that pod, and deleting it turns every exec into a
404 on the ``pods/exec`` subresource, which a campaign reports as a simulator that could
not be asked about its world rather than as the stop it was.
"""

from types import SimpleNamespace

import pytest

from robovast.execution.cluster_execution import cluster_execution


@pytest.fixture(name="cluster")
def _cluster(monkeypatch):
    """A cluster with nothing in it, and a record of the aux reaps that were asked for."""
    reaped = []
    empty = SimpleNamespace(items=[])

    class _Api:
        def __getattr__(self, _name):
            return lambda *a, **k: empty

    monkeypatch.setattr(cluster_execution.client, "CoreV1Api", _Api)
    monkeypatch.setattr(cluster_execution.client, "BatchV1Api", _Api)
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.kube_client.load_kube_config",
        lambda **k: None)
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.container_runner.cleanup_aux_pods",
        lambda **k: reaped.append(k))
    return SimpleNamespace(reaped=reaped)


def test_a_reaper_collects_the_campaigns_aux_pods(cluster):
    """Nothing else will: the span that owned them is gone by the time this runs."""
    cluster_execution.cleanup_cluster_campaign(namespace="ns", campaign="camp-a")

    assert [r["campaign"] for r in cluster.reaped] == ["camp-a"]


def test_a_stop_leaves_them_to_the_span_that_is_using_them(cluster):
    cluster_execution.cleanup_cluster_campaign(namespace="ns", campaign="camp-a",
                                               aux=False)

    assert cluster.reaped == []
