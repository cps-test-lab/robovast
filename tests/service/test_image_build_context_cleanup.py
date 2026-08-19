# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Lifecycle of an in-cluster build's staged context.

The BuildKit Job self-destructs at ``ttlSecondsAfterFinished``, but the context it
mirrored in (a full copy of the project dir) is ours to remove — nothing else will.
Two mechanisms are covered here: the context is dropped the moment a build is seen to
be terminal, and any context whose Job is gone is swept at the next build. The sweep's
one hazard is deleting a *sibling's* context: it is staged before its Job exists, so
"no Job" alone does not mean stale.
"""

import tempfile
import types

import pytest

from robovast.execution.cluster_execution import in_pod_storage
from robovast.execution.cluster_execution.cluster_image_build import (BUILD_CONTEXT_BUCKET,
                                                                      context_prefix,
                                                                      discard_context,
                                                                      staged_context_build_ids)
from robovast.execution.cluster_execution.cluster_service import ClusterService
from robovast.service.interface import ImageBuildStatus
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore


class _FakeStorage:
    """Records prefix deletions; lists whatever keys it was seeded with."""

    def __init__(self, keys=()):
        self.keys = list(keys)
        self.deleted = []

    def upload_dir(self, local_dir, bucket, prefix=""):
        self.keys.append(f"{prefix.rstrip('/')}/Dockerfile")
        return 1

    def list_keys(self, bucket, prefix=""):
        head = prefix.rstrip("/") + "/"
        return [k for k in self.keys if k.startswith(head)]

    def delete_prefix(self, bucket, prefix):
        self.deleted.append((bucket, prefix))
        head = prefix.rstrip("/") + "/"
        removed = [k for k in self.keys if k.startswith(head)]
        self.keys = [k for k in self.keys if k not in removed]
        return len(removed)


def _batch_with(*build_ids):
    jobs = [types.SimpleNamespace(
        metadata=types.SimpleNamespace(labels={"jobgroup": "image-builds",
                                               "build-id": bid}))
            for bid in build_ids]

    class _Batch:
        def list_namespaced_job(self, namespace, label_selector):
            assert label_selector == "jobgroup=image-builds"
            return types.SimpleNamespace(items=jobs)
    return _Batch()


@pytest.fixture
def cs():
    store = WorkspaceStore(registry=WorkspaceRegistry(root=tempfile.mkdtemp()))
    return ClusterService(namespace="ns1", cluster_config_name="rke2",
                          cluster_config_kwargs={}, store=store,
                          reap_on_start=False)


def _record(cs, build_id, *, done):
    status = ImageBuildStatus(build_id=build_id, tag="foo", phase="building", done=done)
    cs._image_build_state()[build_id] = {
        "tag": "foo", "image_ref": "reg/foo:h", "hash": "h", "status": status}
    return status


# ---------------------------------------------------------------------------
# the prefix layout, and reading build ids back out of it
# ---------------------------------------------------------------------------

def test_build_ids_are_recovered_from_the_staged_keys():
    """The listing is the record — no side table to drift from the object store."""
    storage = _FakeStorage([
        "image-builds/imgbuild-a-111/Dockerfile",
        "image-builds/imgbuild-a-111/src/x.py",
        "image-builds/imgbuild-b-222/Dockerfile",
        "camp-2026-07-17-120000/data.db",   # a campaign's results, not a context
    ])
    assert staged_context_build_ids(storage, "bkt") == {"imgbuild-a-111",
                                                       "imgbuild-b-222"}


def test_discard_context_deletes_exactly_that_build_prefix():
    storage = _FakeStorage(["image-builds/imgbuild-a-111/Dockerfile",
                            "image-builds/imgbuild-a-1119/Dockerfile"])
    assert discard_context(storage, "bkt", "imgbuild-a-111") == 1
    assert storage.deleted == [("bkt", context_prefix("imgbuild-a-111"))]
    # The trailing slash the client appends keeps a sibling id with a longer name safe.
    assert storage.keys == ["image-builds/imgbuild-a-1119/Dockerfile"]


def test_delete_prefix_refuses_an_empty_prefix():
    """Shared-bucket deployments keep campaign results in the same bucket."""
    for empty in ("", "   ", "/"):
        with pytest.raises(ValueError, match="empty prefix"):
            in_pod_storage._delete_key_prefix("bkt", empty)


# ---------------------------------------------------------------------------
# discard on the terminal transition
# ---------------------------------------------------------------------------

def test_finished_build_discards_its_context_once(cs, monkeypatch):
    storage = _FakeStorage(["image-builds/imgbuild-foo-abc/Dockerfile"])
    monkeypatch.setattr(in_pod_storage, "storage_client_for", lambda cfg: storage)
    monkeypatch.setattr(cs, "_existing_build_job", lambda bid: "succeeded")
    _record(cs, "imgbuild-foo-abc", done=False)

    assert cs.get_image_build_status("imgbuild-foo-abc").done
    assert storage.deleted == [(BUILD_CONTEXT_BUCKET,
                                context_prefix("imgbuild-foo-abc"))]

    # A second poll returns the cached done record and must not delete again.
    cs.get_image_build_status("imgbuild-foo-abc")
    assert len(storage.deleted) == 1


def test_failed_build_also_discards_its_context(cs, monkeypatch):
    """A failure is diagnosed from the build log, not from the staged tree."""
    storage = _FakeStorage(["image-builds/imgbuild-foo-abc/Dockerfile"])
    monkeypatch.setattr(in_pod_storage, "storage_client_for", lambda cfg: storage)
    monkeypatch.setattr(cs, "_existing_build_job", lambda bid: "failed")
    monkeypatch.setattr(cs, "_build_error", lambda bid, tag: None)
    _record(cs, "imgbuild-foo-abc", done=False)

    assert cs.get_image_build_status("imgbuild-foo-abc").phase == "failed"
    assert storage.deleted == [(BUILD_CONTEXT_BUCKET,
                                context_prefix("imgbuild-foo-abc"))]


def test_a_still_running_build_keeps_its_context(cs, monkeypatch):
    storage = _FakeStorage(["image-builds/imgbuild-foo-abc/Dockerfile"])
    monkeypatch.setattr(in_pod_storage, "storage_client_for", lambda cfg: storage)
    monkeypatch.setattr(cs, "_existing_build_job", lambda bid: "running")
    _record(cs, "imgbuild-foo-abc", done=False)

    assert not cs.get_image_build_status("imgbuild-foo-abc").done
    assert storage.deleted == []


def test_a_build_from_a_previous_service_instance_is_retired_too(cs, monkeypatch):
    """After a restart there is no in-process record to memoize the transition, so the
    poll itself has to retire the context rather than wait for the next build."""
    storage = _FakeStorage(["image-builds/imgbuild-foo-abc/Dockerfile"])
    monkeypatch.setattr(in_pod_storage, "storage_client_for", lambda cfg: storage)
    monkeypatch.setattr(cs, "_existing_build_job", lambda bid: "succeeded")

    assert cs.get_image_build_status("imgbuild-foo-abc").done
    assert storage.deleted == [(BUILD_CONTEXT_BUCKET,
                                context_prefix("imgbuild-foo-abc"))]


def test_a_failing_discard_does_not_fail_the_status_read(cs, monkeypatch):
    """Cleanup is best-effort: a leftover context must not break a finished build."""
    def boom(cfg):
        raise RuntimeError("s3 unreachable")
    monkeypatch.setattr(in_pod_storage, "storage_client_for", boom)
    monkeypatch.setattr(cs, "_existing_build_job", lambda bid: "succeeded")
    _record(cs, "imgbuild-foo-abc", done=False)

    assert cs.get_image_build_status("imgbuild-foo-abc").phase == "succeeded"


# ---------------------------------------------------------------------------
# the sweep
# ---------------------------------------------------------------------------

def test_sweep_removes_contexts_whose_job_is_gone(cs, monkeypatch):
    """No Job means the build ended over a TTL ago, or died with a previous service."""
    storage = _FakeStorage(["image-builds/imgbuild-orphan-1/Dockerfile",
                            "image-builds/imgbuild-live-2/Dockerfile"])
    monkeypatch.setattr(in_pod_storage, "storage_client_for", lambda cfg: storage)
    monkeypatch.setattr(cs, "_k8s_batch", lambda: _batch_with("imgbuild-live-2"))

    cs._sweep_build_contexts(object(), "bkt")
    assert storage.deleted == [("bkt", context_prefix("imgbuild-orphan-1"))]


def test_sweep_holds_back_a_build_this_process_still_has_in_flight(cs, monkeypatch):
    """A sibling request stages its context *before* creating its Job — deleting it
    then would starve that build's init container of the tree it is about to mirror."""
    storage = _FakeStorage(["image-builds/imgbuild-staging-1/Dockerfile"])
    monkeypatch.setattr(in_pod_storage, "storage_client_for", lambda cfg: storage)
    monkeypatch.setattr(cs, "_k8s_batch", lambda: _batch_with())  # no Jobs at all
    _record(cs, "imgbuild-staging-1", done=False)

    cs._sweep_build_contexts(object(), "bkt")
    assert storage.deleted == []


def test_sweep_survives_an_unreachable_cluster_or_store(cs, monkeypatch):
    def boom(cfg):
        raise RuntimeError("s3 unreachable")
    monkeypatch.setattr(in_pod_storage, "storage_client_for", boom)
    cs._sweep_build_contexts(object(), "bkt")  # no raise


# ---------------------------------------------------------------------------
# where the sweep sits in a submit
# ---------------------------------------------------------------------------

def _submit_stubs(cs, monkeypatch, storage):
    """Stub a submit down to its context handling (no registry, no kube, no docker)."""
    from robovast.execution.cluster_execution import cluster_image_build
    monkeypatch.setattr(in_pod_storage, "storage_client_for", lambda cfg: storage)
    # The ref now comes from the lane's image store -- one resolution shared by the submit
    # and by every later "is it there?", so the two cannot disagree about this image's name.
    from robovast.service.image_store import ImageRef
    # Installing a store is how a lane supplies one, so a test supplies one the same way.
    monkeypatch.setattr(
        cs, "_image_store",
        types.SimpleNamespace(ref_for=lambda spec_, dir_: ImageRef(
            ref="reg/foo:h", identity="build:foo@h", build_id="imgbuild-foo-h",
            image_hash="h")), raising=False)
    monkeypatch.setattr(cs, "_existing_build_job", lambda bid: None)
    monkeypatch.setattr(cs, "_k8s_batch", lambda: _batch_with())
    monkeypatch.setattr("robovast.service.image_build.generate_dockerfile",
                        lambda spec, project_dir, base_ref: "FROM base")
    monkeypatch.setattr(cluster_image_build, "build_job_manifest",
                        lambda **kw: {"metadata": {"name": kw["build_id"]}})
    cfg = types.SimpleNamespace(
        get_s3_credentials=lambda: ("ak", "sk"),
        get_s3_endpoint=lambda: "http://robovast:9000",
        get_host_aliases=lambda: None)
    spec = types.SimpleNamespace(tag="foo", base_image="base:1")
    registry = types.SimpleNamespace(registry_prefix="reg", push_secret_name="push",
                                     pull_secret_name="pull",
                                     insecure=False, ca_configmap_name="",
                                     base_experiment_image="")
    return cfg, spec, registry


def test_submit_sweeps_even_when_the_image_is_already_built(cs, monkeypatch):
    """The sweep runs before the cache probe: a project whose image never changes
    would otherwise stop cleaning up the day it started hitting the cache."""
    storage = _FakeStorage(["image-builds/imgbuild-orphan-1/Dockerfile"])
    cfg, spec, registry = _submit_stubs(cs, monkeypatch, storage)
    monkeypatch.setattr(cs, "_registry_has_image", lambda found: True)

    assert cs._start_cluster_build(spec, "/proj", cfg, registry, "bkt").cached
    assert storage.deleted == [("bkt", context_prefix("imgbuild-orphan-1"))]


def test_a_build_is_in_flight_before_its_context_is_staged(cs, monkeypatch):
    """The in-flight record must exist by the time the upload starts — that is what
    stops a concurrent sweep from deleting a context whose Job does not exist yet."""
    from robovast.execution.cluster_execution import cluster_image_build
    storage = _FakeStorage()
    cfg, spec, registry = _submit_stubs(cs, monkeypatch, storage)
    monkeypatch.setattr(cs, "_registry_has_image", lambda found: False)
    seen = {}

    def fake_stage(storage_client, bucket, prefix, project_dir, dockerfile):
        record = cs._image_build_state().get("imgbuild-foo-h")
        seen["in_flight"] = bool(record) and not record["status"].done
        seen["prefix"] = prefix
    monkeypatch.setattr(cluster_image_build, "stage_context_to_s3", fake_stage)

    class _Batch:
        def list_namespaced_job(self, namespace, label_selector=None):
            return types.SimpleNamespace(items=[])

        def create_namespaced_job(self, namespace, manifest):
            return None
    monkeypatch.setattr(cs, "_k8s_batch", lambda: _Batch())

    ref = cs._start_cluster_build(spec, "/proj", cfg, registry, "bkt")
    assert seen == {"in_flight": True, "prefix": context_prefix("imgbuild-foo-h")}
    assert cs.get_image_build_status(ref.build_id).phase == "building"


def test_a_submit_that_dies_before_its_job_exists_takes_its_context_with_it(
        cs, monkeypatch):
    """Otherwise the in-flight record that protects the context from the sweep would
    keep protecting it for the service's whole lifetime."""
    storage = _FakeStorage()
    cfg, spec, registry = _submit_stubs(cs, monkeypatch, storage)
    monkeypatch.setattr(cs, "_registry_has_image", lambda found: False)

    class _Batch:
        def list_namespaced_job(self, namespace, label_selector=None):
            return types.SimpleNamespace(items=[])

        def create_namespaced_job(self, namespace, manifest):
            raise RuntimeError("the API server said no")
    monkeypatch.setattr(cs, "_k8s_batch", lambda: _Batch())

    with pytest.raises(RuntimeError, match="said no"):
        cs._start_cluster_build(spec, "/proj", cfg, registry, "bkt")

    assert storage.deleted == [("bkt", context_prefix("imgbuild-foo-h"))]
    # And the record no longer holds the sweep back.
    assert cs.get_image_build_status("imgbuild-foo-h").done
