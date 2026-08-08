# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Kueue's CRD self-healing, and saying which link of the chain is broken.

A missing Kueue CRD is the worst shape of failure this system has: a job labelled into
the queue is created **suspended**, so nothing errors. The Job is "active", the campaign
log says "still running", and ``activeDeadlineSeconds`` cannot save it because the timer
does not run while a Job is suspended. It simply never starts.

That happened here: ``clusterqueues.kueue.x-k8s.io`` was absent while the other ten Kueue
CRDs were present, and every campaign parked forever. Three gaps made it worse than it
had to be, and each is pinned below.

No cluster needed: helm/kubectl are subprocesses and the Kueue objects are read through
one API client, so both fake cleanly.
"""

import types

import pytest

from robovast.execution.cluster_execution import kubernetes_kueue as kk


# -- the recovery reports why it failed --------------------------------------


def test_a_failed_recovery_says_why(monkeypatch):
    """Without the reason, the operator is told the CRDs are missing "even after
    recovery" -- which is the one sentence that does not say what to do next."""
    monkeypatch.setattr(kk, "_wait_for_kueue_crds",
                        lambda *_a, **_k: ["clusterqueues.kueue.x-k8s.io"])
    monkeypatch.setattr(kk, "_force_apply_kueue_crds",
                        lambda *_a: (False, "the CRD is invalid: metadata.annotations: Too long"))

    with pytest.raises(RuntimeError) as excinfo:
        kk._ensure_kueue_crds([], [], timeout=1)

    message = str(excinfo.value)
    assert "clusterqueues.kueue.x-k8s.io" in message
    assert "Too long" in message, "the recovery's own failure was swallowed"


def test_a_successful_recovery_does_not_raise(monkeypatch):
    calls = {"n": 0}

    def _wait(*_a, **_k):
        calls["n"] += 1
        return ["clusterqueues.kueue.x-k8s.io"] if calls["n"] == 1 else []

    monkeypatch.setattr(kk, "_wait_for_kueue_crds", _wait)
    monkeypatch.setattr(kk, "_force_apply_kueue_crds", lambda *_a: (True, ""))
    kk._ensure_kueue_crds([], [], timeout=1)  # must not raise
    assert calls["n"] == 2, "the CRDs were not re-checked after the repair"


# -- applying the queues heals the CRDs first --------------------------------


def test_applying_the_queues_heals_the_crds_first(monkeypatch):
    """The gap that made this unrecoverable.

    The self-heal only ran inside the Helm install. Helm never re-creates a chart's
    ``crds/`` on upgrade, so once a CRD vanished after a successful setup, re-running
    setup could not restore it -- and setup is exactly what every error message tells
    you to run. Healing here makes that advice true.
    """
    order = []
    monkeypatch.setattr(kk, "get_cluster_allocatable_resources",
                        lambda **_k: (96, "125Gi"))
    monkeypatch.setattr(kk, "_ensure_kueue_crds",
                        lambda *_a, **_k: order.append("heal"))

    def _fake_run(cmd, **_kw):
        if cmd[0] == "kubectl" and "apply" in cmd:
            order.append("apply-queues")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(kk.subprocess, "run", _fake_run)
    kk.apply_kueue_queues(namespace="default", kube_context="local")

    assert order == ["heal", "apply-queues"], \
        "the queues were applied without first ensuring their CRDs exist"


# -- naming the broken link ---------------------------------------------------


class _Api:
    """A CustomObjectsApi whose reads are scripted per plural."""

    def __init__(self, objects, registered=True):
        self._objects = objects
        self._registered = registered

    def get_namespaced_custom_object(self, *, plural, name, **_kw):
        return self._get(plural, name)

    def get_cluster_custom_object(self, *, plural, name, **_kw):
        return self._get(plural, name)

    def _get(self, plural, name):
        obj = self._objects.get((plural, name))
        if obj is None:
            raise kk.client.rest.ApiException(status=404, reason="Not Found")
        return obj

    def list_cluster_custom_object(self, **_kw):
        if not self._registered:
            raise kk.client.rest.ApiException(status=404, reason="Not Found")
        return {"items": []}


def _local_queue():
    return {"spec": {"clusterQueue": kk.CLUSTER_QUEUE_NAME}}


def test_a_missing_crd_is_named_as_such(monkeypatch):
    """"ClusterQueue does not exist" reads identically whether the object is gone or the
    whole kind is uninstalled -- but only the second means re-applying the queue cannot
    work until the CRD is restored. Following the wrong remedy is a dead end."""
    from robovast.common.errors import CampaignConfigError

    api = _Api({("localqueues", kk.KUEUE_QUEUE_NAME): _local_queue()}, registered=False)
    monkeypatch.setattr(kk.client, "CustomObjectsApi", lambda: api)

    with pytest.raises(CampaignConfigError) as excinfo:
        kk._check_kueue_admission("default")
    assert "CRD itself is not registered" in str(excinfo.value)


def test_a_merely_absent_cluster_queue_is_not_blamed_on_the_crd(monkeypatch):
    """The ordinary case must keep its ordinary remedy."""
    from robovast.common.errors import CampaignConfigError

    api = _Api({("localqueues", kk.KUEUE_QUEUE_NAME): _local_queue()}, registered=True)
    monkeypatch.setattr(kk.client, "CustomObjectsApi", lambda: api)

    with pytest.raises(CampaignConfigError) as excinfo:
        kk._check_kueue_admission("default")
    message = str(excinfo.value)
    assert "does not exist" in message
    assert "CRD itself is not registered" not in message


def test_an_unreadable_crd_check_does_not_accuse_the_install(monkeypatch):
    """A check that cannot answer must not report a broken install -- that sends someone
    to reinstall Kueue over what is really an RBAC gap."""
    class _Broken(_Api):
        def list_cluster_custom_object(self, **_kw):
            raise RuntimeError("connection reset")

    api = _Broken({("localqueues", kk.KUEUE_QUEUE_NAME): _local_queue()})
    assert kk._crd_registered(api, "clusterqueues") is True
