# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A built image is pulled onto a node before anything waits for it.

Nothing reads a prewarm back -- there is no status field, no wait, no poll -- which is what
keeps it off every interface, and also what makes it invisible when it silently stops
working. These tests are therefore the only thing standing between "the node is warm" and
"the node is not warm and nobody can tell". Two groups matter most:

* **the manifest self-collects.** ``ttlSecondsAfterFinished`` alone does not achieve that,
  because TTL only starts once a Job is terminal and a pod stuck in ``ImagePullBackOff``
  leaves a ``backoffLimit: 0`` Job ``active`` forever. A prewarm that leaks on failure is
  worse than no prewarm, so the deadline is asserted directly.
* **a prewarm never breaks its caller.** It is an optimization; failing one must leave
  exactly the situation that held before the feature existed.
"""

import tempfile
import types

import pytest

from robovast.execution.cluster_execution.cluster_service import ClusterService
from robovast.execution.cluster_execution.image_warm import (WARM_DEADLINE_SECONDS,
                                                             WARM_JOBGROUP,
                                                             WARM_TTL_SECONDS,
                                                             warm_id_for,
                                                             warm_job_manifest)
from robovast.service.interface import ImageBuildStatus
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore

BUILD = "imgbuild-sut-abc123"
REF = "harbor.example.de/robovast/sut:abc123"


@pytest.fixture
def cs():
    store = WorkspaceStore(registry=WorkspaceRegistry(root=tempfile.mkdtemp()))
    return ClusterService(namespace="ns1", cluster_config_name="rke2",
                          cluster_config_kwargs={}, store=store, reap_on_start=False)


class _Batch:
    """A batch API that records the Jobs it was asked to create."""

    def __init__(self, fail_with=None):
        self.created = []
        self._fail_with = fail_with

    def create_namespaced_job(self, namespace, manifest):
        if self._fail_with is not None:
            raise self._fail_with
        self.created.append((namespace, manifest))

    @property
    def names(self):
        return [m["metadata"]["name"] for _ns, m in self.created]

    @property
    def images(self):
        return [m["spec"]["template"]["spec"]["containers"][0]["image"]
                for _ns, m in self.created]


def _wire(cs, monkeypatch, batch, *, pull_secret="robovast-registry-push"):
    monkeypatch.setattr(cs, "_k8s_batch", lambda: batch)
    monkeypatch.setattr(cs, "_registry_pull_secret", lambda: pull_secret)
    return batch


def _api_exception(status):
    from kubernetes import client
    e = client.exceptions.ApiException(status=status)
    return e


# ---------------------------------------------------------------------------
# the manifest collects itself
# ---------------------------------------------------------------------------

def test_the_job_has_a_deadline_as_well_as_a_ttl():
    """The bug this avoids: TTL never fires on a Job that never becomes terminal.

    With ``backoffLimit: 0`` and no deadline, a pod wedged in ImagePullBackOff leaves both
    of the Job's counters at zero, so it stays ``active`` and is never reaped. The build
    path carries a whole ``blocked``-phase probe because of exactly this; a prewarm has
    nobody watching it and so must terminate itself.
    """
    spec = warm_job_manifest(image_ref=REF, namespace="ns1")["spec"]
    assert spec["activeDeadlineSeconds"] == WARM_DEADLINE_SECONDS
    assert spec["ttlSecondsAfterFinished"] == WARM_TTL_SECONDS
    assert spec["backoffLimit"] == 0
    assert spec["template"]["spec"]["restartPolicy"] == "Never"


def test_the_job_is_not_submitted_to_kueue():
    """A prewarm admitted behind a full sweep warms the node after the thing that needed it.

    Bypassing the queue is what the build Job already does; campaign and postprocessing Jobs
    are the ones that carry the label.
    """
    manifest = warm_job_manifest(image_ref=REF, namespace="ns1")
    for labels in (manifest["metadata"]["labels"],
                   manifest["spec"]["template"]["metadata"]["labels"]):
        assert "kueue.x-k8s.io/queue-name" not in labels


def test_the_jobgroup_is_not_the_build_jobgroup():
    """``_sweep_build_contexts`` selects ``jobgroup=image-builds`` to decide which staged
    contexts are still live. A prewarm stages nothing, so appearing there would make it look
    like a build in flight and hold a dead context back from the sweep."""
    manifest = warm_job_manifest(image_ref=REF, namespace="ns1")
    assert manifest["metadata"]["labels"]["jobgroup"] == WARM_JOBGROUP
    assert WARM_JOBGROUP != "image-builds"


def test_the_pod_carries_the_registry_pull_secret():
    """Without it the pod cannot pull from a private registry, so the prewarm does nothing --
    on exactly the deployment that needed it. It fails silently, since nothing reads it."""
    spec = warm_job_manifest(image_ref=REF, namespace="ns1",
                             pull_secret_name="reg-push")["spec"]["template"]["spec"]
    assert spec["imagePullSecrets"] == [{"name": "reg-push"}]
    assert spec["containers"][0]["imagePullPolicy"] == "IfNotPresent"


def test_no_pull_secret_means_no_empty_reference():
    """Naming a Secret that does not exist keeps the pod from starting at all."""
    spec = warm_job_manifest(image_ref=REF, namespace="ns1")["spec"]["template"]["spec"]
    assert "imagePullSecrets" not in spec


def test_the_name_is_deterministic_dns_safe_and_ref_specific():
    """Idempotency rests entirely on this: same ref -> same name -> a 409 on the repeat."""
    assert warm_id_for(REF) == warm_id_for(REF)
    assert warm_id_for(REF) != warm_id_for("harbor.example.de/robovast/sut:def456")
    # A digest ref and an underscored tag are both legal inputs and neither may escape.
    for ref in (REF, "harbor.example.de/robovast/my_sut@sha256:" + "a" * 64,
                "ghcr.io/cps-test-lab/robovast-roqsim:latest"):
        name = warm_id_for(ref)
        assert len(name) < 64, name
        assert name.replace("-", "").isalnum() and name.islower(), name
        assert not name.startswith("-") and not name.endswith("-"), name


# ---------------------------------------------------------------------------
# it fires where the service learns an image exists
# ---------------------------------------------------------------------------

def test_a_finished_build_warms_the_image_it_produced(cs, monkeypatch):
    """The fire point that earns the feature: the image exists, nobody has pulled it, and
    what needs pulling is the layers this build added."""
    batch = _wire(cs, monkeypatch, _Batch())
    monkeypatch.setattr(cs, "_existing_build_job", lambda bid: "succeeded")
    monkeypatch.setattr(cs, "_retire_build_context", lambda bid: None)
    status = ImageBuildStatus(build_id=BUILD, tag="sut", phase="building", done=False)
    cs._image_build_state()[BUILD] = {
        "tag": "sut", "image_ref": REF, "hash": "abc123", "status": status}

    assert cs.get_image_build_status(BUILD).phase == "succeeded"
    assert batch.images == [REF]


def test_a_failed_build_warms_nothing(cs, monkeypatch):
    """There is nothing in the registry to pull, so a Job here would only ImagePullBackOff
    until its deadline."""
    batch = _wire(cs, monkeypatch, _Batch())
    monkeypatch.setattr(cs, "_existing_build_job", lambda bid: "failed")
    monkeypatch.setattr(cs, "_retire_build_context", lambda bid: None)
    monkeypatch.setattr(cs, "_build_error", lambda bid, spec=None: None)
    status = ImageBuildStatus(build_id=BUILD, tag="sut", phase="building", done=False)
    cs._image_build_state()[BUILD] = {
        "tag": "sut", "image_ref": REF, "hash": "abc123", "status": status}

    assert cs.get_image_build_status(BUILD).phase == "failed"
    assert batch.created == []


def test_polling_a_finished_build_does_not_warm_again(cs, monkeypatch):
    """A done record returns before the transition block, so the warm runs once."""
    batch = _wire(cs, monkeypatch, _Batch())
    monkeypatch.setattr(cs, "_existing_build_job", lambda bid: "succeeded")
    monkeypatch.setattr(cs, "_retire_build_context", lambda bid: None)
    status = ImageBuildStatus(build_id=BUILD, tag="sut", phase="building", done=False)
    cs._image_build_state()[BUILD] = {
        "tag": "sut", "image_ref": REF, "hash": "abc123", "status": status}

    for _ in range(3):
        cs.get_image_build_status(BUILD)
    assert len(batch.created) == 1


# ---------------------------------------------------------------------------
# a prewarm never breaks its caller
# ---------------------------------------------------------------------------

def test_an_already_existing_prewarm_is_not_an_error(cs, monkeypatch):
    """Idempotency: another caller (or another service instance) got here first."""
    _wire(cs, monkeypatch, _Batch(fail_with=_api_exception(409)))
    monkeypatch.setattr(cs, "_existing_build_job", lambda bid: "succeeded")
    monkeypatch.setattr(cs, "_retire_build_context", lambda bid: None)
    status = ImageBuildStatus(build_id=BUILD, tag="sut", phase="building", done=False)
    cs._image_build_state()[BUILD] = {
        "tag": "sut", "image_ref": REF, "hash": "abc123", "status": status}

    assert cs.get_image_build_status(BUILD).phase == "succeeded"


def test_a_prewarm_that_cannot_be_created_leaves_the_build_succeeded(cs, monkeypatch,
                                                                    caplog):
    """The whole trade: a failed prewarm is a slow pod later, which is where we started.
    Turning it into a failed build would make the optimization a liability.

    It must still say so, because nothing reads a prewarm back -- the log is the only place
    a permanently broken one can surface.
    """
    _wire(cs, monkeypatch, _Batch(fail_with=_api_exception(403)))
    monkeypatch.setattr(cs, "_existing_build_job", lambda bid: "succeeded")
    monkeypatch.setattr(cs, "_retire_build_context", lambda bid: None)
    status = ImageBuildStatus(build_id=BUILD, tag="sut", phase="building", done=False)
    cs._image_build_state()[BUILD] = {
        "tag": "sut", "image_ref": REF, "hash": "abc123", "status": status}

    with caplog.at_level("WARNING"):
        result = cs.get_image_build_status(BUILD)
    assert result.phase == "succeeded" and result.done is True
    assert any("prewarm" in r.message.lower() or "prewarm" in r.getMessage().lower()
               for r in caplog.records), caplog.text


def test_an_unreachable_cluster_does_not_fail_the_build(cs, monkeypatch):
    """`_k8s_batch` itself can raise -- the guard is around the whole call, not just create."""
    monkeypatch.setattr(cs, "_k8s_batch",
                        lambda: (_ for _ in ()).throw(RuntimeError("no cluster")))
    monkeypatch.setattr(cs, "_registry_pull_secret", lambda: "")
    monkeypatch.setattr(cs, "_existing_build_job", lambda bid: "succeeded")
    monkeypatch.setattr(cs, "_retire_build_context", lambda bid: None)
    status = ImageBuildStatus(build_id=BUILD, tag="sut", phase="building", done=False)
    cs._image_build_state()[BUILD] = {
        "tag": "sut", "image_ref": REF, "hash": "abc123", "status": status}

    assert cs.get_image_build_status(BUILD).phase == "succeeded"


def test_the_restart_branch_deliberately_warms_nothing(cs, monkeypatch):
    """It holds only a build_id, and `build_id_for` does not reverse into a ref: it
    lowercases and folds `_` to `-`, which `concrete_image_ref` does not. A guessed ref
    would ImagePullBackOff until its deadline, warming nothing and reporting nothing. The
    next submit takes the cache-hit path and warms from a resolved ref instead."""
    batch = _wire(cs, monkeypatch, _Batch())
    monkeypatch.setattr(cs, "_existing_build_job", lambda bid: "succeeded")
    monkeypatch.setattr(cs, "_retire_build_context", lambda bid: None)
    # No in-process record: the service restarted since the build was submitted.

    assert cs.get_image_build_status(BUILD).phase == "succeeded"
    assert batch.created == []


# ---------------------------------------------------------------------------
# the cache hit -- the coldest case, and the one that looks fastest
# ---------------------------------------------------------------------------

def _cache_hit_setup(cs, monkeypatch, batch):
    """A submit whose image the registry already has, so no build is created."""
    from robovast.service.image_store import ImageRef
    found = ImageRef(ref=REF, identity="build:sut@abc123", build_id=BUILD,
                     image_hash="abc123")
    monkeypatch.setattr(cs, "_image_store", types.SimpleNamespace(
        ref_for=lambda spec, project_dir: found), raising=False)
    monkeypatch.setattr(cs, "_sweep_build_contexts", lambda cfg, bucket: None)
    monkeypatch.setattr(cs, "_registry_has_image", lambda f: True)
    _wire(cs, monkeypatch, batch)
    return types.SimpleNamespace(tag="sut", base_image="")


def test_a_cache_hit_still_warms(cs, monkeypatch):
    """Nothing gets built, so this is the only chance to warm -- and it is the coldest case
    there is: the image may have been pushed weeks ago, by a service that has restarted,
    onto a node that has rebooted since. Before this it was the path that looked fastest
    and behaved slowest."""
    batch = _Batch()
    spec = _cache_hit_setup(cs, monkeypatch, batch)

    ref = cs._start_cluster_build(spec, "/proj", object(), object(), "bucket")
    assert ref.cached is True
    assert batch.images == [REF]


def test_a_cache_hit_whose_prewarm_fails_still_reports_cached(cs, monkeypatch):
    batch = _Batch(fail_with=_api_exception(403))
    spec = _cache_hit_setup(cs, monkeypatch, batch)

    assert cs._start_cluster_build(spec, "/proj", object(), object(), "bucket").cached
