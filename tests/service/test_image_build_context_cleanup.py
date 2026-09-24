# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Lifecycle of an in-cluster build's staged context.

The BuildKit Job self-destructs at ``ttlSecondsAfterFinished``, but the context it
fetched (a full copy of the project dir, staged on the service's disk) is ours to remove
— nothing else will. Two mechanisms are covered here: the context is dropped the moment
a build is seen to be terminal, and any context whose Job is gone is swept at the next
build. The sweep's one hazard is deleting a *sibling's* context: it is staged before its
Job exists, so "no Job" alone does not mean stale.
"""

import tempfile
import types

import pytest

from robovast.execution.cluster_execution.cluster_image_build import (BUILD_CONTEXT_PREFIX,
                                                                      context_slot,
                                                                      stage_context,
                                                                      staged_context_build_ids)
from robovast.execution.cluster_execution.cluster_service import ClusterService
from robovast.service.interface import ImageBuildStatus
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore
from robovast.service.image_build import BuildSpec


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
def cs(monkeypatch, tmp_path):
    store = WorkspaceStore(registry=WorkspaceRegistry(root=tempfile.mkdtemp()))
    svc = ClusterService(namespace="ns1", cluster_config_name="rke2",
                         cluster_config_kwargs={}, store=store,
                         reap_on_start=False, results_dir=str(tmp_path / "results"))
    # A status read probes the build POD as well as its Job, and that probe is a real
    # Kubernetes GET: unstubbed it goes to whatever cluster the developer's kubeconfig
    # names and waits for it to time out, with a result that depends on which cluster is
    # configured. `(None, None)` is the documented "the pod is fine, or there is none
    # yet", which is the state every test in this file means; one that cares about a
    # blocked or failed pod overrides it.
    monkeypatch.setattr(svc, "_build_pod_verdict", lambda build_id: (None, None))
    # A build that just succeeded is warmed onto a node, which resolves a registry pull
    # secret over the API -- another real cluster call, on the other path through the same
    # method. Pre-pulling an image is a side effect no test in this file is about; `_warm`
    # exists as a seam precisely so it can be one line at each call site, and here that
    # makes it one line to silence.
    monkeypatch.setattr(svc, "_warm", lambda image_ref: None)
    return svc


def _staged(cs, *build_ids):
    """Stage a Dockerfile for each build id, the way a submit leaves it."""
    for build_id in build_ids:
        root = cs.staged_dir(context_slot(build_id))
        root.mkdir(parents=True)
        (root / "Dockerfile").write_text("FROM base\n")


def _staged_ids(cs):
    return staged_context_build_ids(cs.staged_dir(BUILD_CONTEXT_PREFIX))


def _record(cs, build_id, *, done):
    status = ImageBuildStatus(build_id=build_id, tag="foo", phase="building", done=done)
    cs._image_build_state()[build_id] = {
        "tag": "foo", "image_ref": "reg/foo:h", "hash": "h", "status": status}
    return status


# ---------------------------------------------------------------------------
# the staged layout, and reading build ids back out of it
# ---------------------------------------------------------------------------

def test_build_ids_are_recovered_from_the_staged_directories(tmp_path):
    """The listing is the record — no side table to drift from the disk."""
    contexts = tmp_path / "_staged" / BUILD_CONTEXT_PREFIX
    for build_id in ("imgbuild-a-111", "imgbuild-b-222"):
        (contexts / build_id / "src").mkdir(parents=True)
        (contexts / build_id / "Dockerfile").write_text("FROM base\n")
    (contexts / "stray-file").write_text("not a context")
    assert staged_context_build_ids(contexts) == {"imgbuild-a-111", "imgbuild-b-222"}


def test_no_contexts_directory_means_nothing_is_staged(tmp_path):
    """A service that never built lists an empty set, not an error."""
    assert staged_context_build_ids(tmp_path / "absent") == set()


def test_the_slot_is_the_prefix_and_the_build_id():
    assert context_slot("imgbuild-a-111") == f"{BUILD_CONTEXT_PREFIX}/imgbuild-a-111"


def test_stage_context_writes_the_project_and_the_dockerfile_into_the_slot(tmp_path):
    project = tmp_path / "proj"
    (project / "src").mkdir(parents=True)
    (project / "src" / "x.py").write_text("print(1)\n")
    staged = tmp_path / "_staged" / context_slot("imgbuild-a-111")

    size = stage_context(staged, project, "FROM base\n")

    assert (staged / "Dockerfile").read_text() == "FROM base\n"
    assert (staged / "src" / "x.py").read_text() == "print(1)\n"
    assert size == len("print(1)\n")


def test_stage_context_replaces_a_tree_already_in_the_slot(tmp_path):
    """A re-submitted build stages exactly this project, not the union with the last."""
    project = tmp_path / "proj"
    project.mkdir()
    (project / "keep.py").write_text("1")
    staged = tmp_path / "_staged" / context_slot("imgbuild-a-111")
    staged.mkdir(parents=True)
    (staged / "stale.py").write_text("old")

    stage_context(staged, project, "FROM base\n")

    assert not (staged / "stale.py").exists()
    assert (staged / "keep.py").exists()


# ---------------------------------------------------------------------------
# discard on the terminal transition
# ---------------------------------------------------------------------------

def test_finished_build_discards_its_context_once(cs, monkeypatch):
    _staged(cs, "imgbuild-foo-abc")
    discards = []
    monkeypatch.setattr(cs, "discard_staged",
                        lambda slot: discards.append(slot) or True)
    monkeypatch.setattr(cs, "_existing_build_job", lambda bid: "succeeded")
    _record(cs, "imgbuild-foo-abc", done=False)

    assert cs.get_image_build_status("imgbuild-foo-abc").done
    assert discards == [context_slot("imgbuild-foo-abc")]

    # A second poll returns the cached done record and must not delete again.
    cs.get_image_build_status("imgbuild-foo-abc")
    assert len(discards) == 1


def test_failed_build_also_discards_its_context(cs, monkeypatch):
    """A failure is diagnosed from the build log, not from the staged tree."""
    _staged(cs, "imgbuild-foo-abc")
    monkeypatch.setattr(cs, "_existing_build_job", lambda bid: "failed")
    monkeypatch.setattr(cs, "_build_error", lambda bid, tag: None)
    _record(cs, "imgbuild-foo-abc", done=False)

    assert cs.get_image_build_status("imgbuild-foo-abc").phase == "failed"
    assert _staged_ids(cs) == set()


def test_a_still_running_build_keeps_its_context(cs, monkeypatch):
    _staged(cs, "imgbuild-foo-abc")
    monkeypatch.setattr(cs, "_existing_build_job", lambda bid: "running")
    _record(cs, "imgbuild-foo-abc", done=False)

    assert not cs.get_image_build_status("imgbuild-foo-abc").done
    assert _staged_ids(cs) == {"imgbuild-foo-abc"}


def test_a_build_from_a_previous_service_instance_is_retired_too(cs, monkeypatch):
    """After a restart there is no in-process record to memoize the transition, so the
    poll itself has to retire the context rather than wait for the next build."""
    _staged(cs, "imgbuild-foo-abc")
    monkeypatch.setattr(cs, "_existing_build_job", lambda bid: "succeeded")

    assert cs.get_image_build_status("imgbuild-foo-abc").done
    assert _staged_ids(cs) == set()


def test_a_failing_discard_does_not_fail_the_status_read(cs, monkeypatch):
    """Cleanup is best-effort: a leftover context must not break a finished build."""
    def boom(slot):
        raise RuntimeError("disk unreachable")
    monkeypatch.setattr(cs, "discard_staged", boom)
    monkeypatch.setattr(cs, "_existing_build_job", lambda bid: "succeeded")
    _record(cs, "imgbuild-foo-abc", done=False)

    assert cs.get_image_build_status("imgbuild-foo-abc").phase == "succeeded"


# ---------------------------------------------------------------------------
# the sweep
# ---------------------------------------------------------------------------

def test_sweep_removes_contexts_whose_job_is_gone(cs, monkeypatch):
    """No Job means the build ended over a TTL ago, or died with a previous service."""
    _staged(cs, "imgbuild-orphan-1", "imgbuild-live-2")
    monkeypatch.setattr(cs, "_k8s_batch", lambda: _batch_with("imgbuild-live-2"))

    cs._sweep_build_contexts()
    assert _staged_ids(cs) == {"imgbuild-live-2"}


def test_sweep_holds_back_a_build_this_process_still_has_in_flight(cs, monkeypatch):
    """A sibling request stages its context *before* creating its Job — deleting it
    then would starve that build's init container of the tree it is about to fetch."""
    _staged(cs, "imgbuild-staging-1")
    monkeypatch.setattr(cs, "_k8s_batch", lambda: _batch_with())  # no Jobs at all
    _record(cs, "imgbuild-staging-1", done=False)

    cs._sweep_build_contexts()
    assert _staged_ids(cs) == {"imgbuild-staging-1"}


def test_sweep_survives_an_unreachable_cluster(cs, monkeypatch):
    def boom():
        raise RuntimeError("cluster unreachable")
    monkeypatch.setattr(cs, "_k8s_batch", boom)
    _staged(cs, "imgbuild-orphan-1")
    cs._sweep_build_contexts()  # no raise
    assert _staged_ids(cs) == {"imgbuild-orphan-1"}, "nothing is removed on a guess"


# ---------------------------------------------------------------------------
# where the sweep sits in a submit
# ---------------------------------------------------------------------------

def _submit_stubs(cs, monkeypatch):
    """Stub a submit down to its context handling (no registry, no kube, no docker)."""
    from robovast.execution.cluster_execution import cluster_image_build
    # The ref comes from the service's image store -- one resolution shared by the submit
    # and by every later "is it there?", so the two cannot disagree about this image's name.
    from robovast.service.image_store import ImageRef
    # Installing a store is how the service supplies one, so a test supplies one the same way.
    monkeypatch.setattr(
        cs, "_image_store",
        types.SimpleNamespace(
            ref_for=lambda spec_, dir_: ImageRef(
                ref="reg/foo:h", identity="build:foo@h", build_id="imgbuild-foo-h",
                image_hash="h"),
            # The submit renders the Dockerfile with the git refs already resolved to
            # commits. Empty here: this spec declares no git specs, and these tests are
            # about the staged context rather than about what the Dockerfile installs.
            resolve_vcs=lambda spec_: {},
            # The submit asks the store for the git credential a private `python_packages`
            # spec would install with. None here -- but the stub has to answer, or the
            # submit dies on an AttributeError well before the context handling these
            # tests are about.
            git_secret_name=lambda: "",
            # A submit asks the store whether the registry would refuse the credential
            # it is about to push with, before it stages anything. False here: these tests
            # are about what a submit does once it has decided to build, and a stub that
            # said otherwise would refuse before reaching any of it.
            push_refused=lambda image_ref: False),
        raising=False)
    # A ready build daemon: these tests are about what a submit does, and without one the
    # submit correctly refuses before it does any of it.
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.buildkitd_deploy.buildkitd_ready",
        lambda namespace: True)
    monkeypatch.setattr(cs, "_existing_build_job", lambda bid: None)
    monkeypatch.setattr(cs, "_k8s_batch", lambda: _batch_with())
    monkeypatch.setattr("robovast.service.image_build.generate_dockerfile",
                        lambda spec, project_dir, base_ref, resolved_vcs=None: "FROM base")
    monkeypatch.setattr(cluster_image_build, "build_job_manifest",
                        lambda **kw: {"metadata": {"name": kw["build_id"]}})
    # The pod is given a token scoped to its slot; the submit mints it from the service's
    # secret, which a test service has not been given.
    monkeypatch.setattr(cs, "scoped_token", lambda scope: f"tok({scope})")
    cfg = types.SimpleNamespace(get_host_aliases=lambda: None)
    spec = BuildSpec(tag="foo", base_image="base:1")
    registry = types.SimpleNamespace(registry_prefix="reg", push_secret_name="push",
                                     pull_secret_name="pull",
                                     insecure=False, ca_configmap_name="",
                                     base_experiment_image="")
    return cfg, spec, registry


def test_submit_sweeps_even_when_the_image_is_already_built(cs, monkeypatch):
    """The sweep runs before the cache probe: a project whose image never changes
    would otherwise stop cleaning up the day it started hitting the cache."""
    _staged(cs, "imgbuild-orphan-1")
    cfg, spec, registry = _submit_stubs(cs, monkeypatch)
    monkeypatch.setattr(cs, "_registry_has_image", lambda found: True)

    assert cs._start_cluster_build(spec, "/proj", cfg, registry).cached
    assert _staged_ids(cs) == set()


def test_a_build_is_in_flight_before_its_context_is_staged(cs, monkeypatch):
    """The in-flight record must exist by the time the copy starts — that is what
    stops a concurrent sweep from deleting a context whose Job does not exist yet."""
    from robovast.execution.cluster_execution import cluster_image_build
    cfg, spec, registry = _submit_stubs(cs, monkeypatch)
    monkeypatch.setattr(cs, "_registry_has_image", lambda found: False)
    seen = {}

    def fake_stage(staged_dir, project_dir, dockerfile):
        record = cs._image_build_state().get("imgbuild-foo-h")
        seen["in_flight"] = bool(record) and not record["status"].done
        seen["staged_dir"] = staged_dir
        return 1234
    monkeypatch.setattr(cluster_image_build, "stage_context", fake_stage)

    class _Batch:
        def list_namespaced_job(self, namespace, label_selector=None):
            return types.SimpleNamespace(items=[])

        def create_namespaced_job(self, namespace, manifest):
            return None
    monkeypatch.setattr(cs, "_k8s_batch", lambda: _Batch())

    ref = cs._start_cluster_build(spec, "/proj", cfg, registry)
    assert seen == {"in_flight": True,
                    "staged_dir": cs.staged_dir(context_slot("imgbuild-foo-h"))}
    assert cs.get_image_build_status(ref.build_id).phase == "building"


def test_the_job_is_given_a_token_for_its_own_context(cs, monkeypatch):
    """What the init container fetches with: a token scoped to this build's slot, and
    not the service's secret."""
    from robovast.execution.cluster_execution import cluster_image_build
    from robovast.execution.cluster_execution.pod_access import staged_scope
    cfg, spec, registry = _submit_stubs(cs, monkeypatch)
    monkeypatch.setattr(cs, "_registry_has_image", lambda found: False)
    monkeypatch.setattr(cluster_image_build, "stage_context", lambda *a, **kw: 1)
    seen = {}
    monkeypatch.setattr(cluster_image_build, "build_job_manifest",
                        lambda **kw: seen.update(kw) or {"metadata": {"name": kw["build_id"]}})

    class _Batch:
        def list_namespaced_job(self, namespace, label_selector=None):
            return types.SimpleNamespace(items=[])

        def create_namespaced_job(self, namespace, manifest):
            return None
    monkeypatch.setattr(cs, "_k8s_batch", lambda: _Batch())

    cs._start_cluster_build(spec, "/proj", cfg, registry)
    assert seen["token"] == f"tok({staged_scope(context_slot('imgbuild-foo-h'))})"
    assert seen["namespace"] == "ns1"


def test_a_refused_push_credential_stops_the_submit_before_it_stages_anything(
        cs, monkeypatch):
    """The failure this check exists to move earlier.

    A build whose push will be rejected is otherwise discovered by doing it: every layer
    built, every package installed, and a 401 at the final step. Nothing upstream says
    so -- the capability flag a client reads reports that a registry is *configured*, and
    the cache probe just above is a manifest read, which a registry may serve while
    refusing to receive one.

    Asserted on what did *not* happen: no context copied, no Job. Those are what the
    check is for -- a message alone would pass while the compute was still spent.
    """
    from robovast.execution.cluster_execution import cluster_image_build
    from robovast.common.errors import ImageBuildFailed

    cfg, spec, registry = _submit_stubs(cs, monkeypatch)
    monkeypatch.setattr(cs, "_registry_has_image", lambda found: False)
    monkeypatch.setattr(cs._image_store, "push_refused", lambda image_ref: True,
                        raising=False)

    def refuse_to_stage(*_a, **_kw):
        raise AssertionError("the context was staged for a build that cannot be pushed")
    monkeypatch.setattr(cluster_image_build, "stage_context", refuse_to_stage)

    class _Batch:
        def list_namespaced_job(self, namespace, label_selector=None):
            return types.SimpleNamespace(items=[])

        def create_namespaced_job(self, namespace, manifest):
            raise AssertionError("a Job was created for a build that cannot be pushed")
    monkeypatch.setattr(cs, "_k8s_batch", lambda: _Batch())

    with pytest.raises(ImageBuildFailed) as excinfo:
        cs._start_cluster_build(spec, "/proj", cfg, registry)

    message = str(excinfo.value)
    assert "registry" in message
    # It has to send the reader to the credential and away from `build:`, which is where
    # the first diagnosis of this went.
    assert "build:" in message and "setup" in message
    assert _staged_ids(cs) == set(), "something was staged for a build that cannot be pushed"


def test_a_registry_that_did_not_answer_does_not_stop_a_submit(cs, monkeypatch):
    """The asymmetry. Only a registry that answered *and* refused blocks a build.

    Turning "could not ask" into a refusal would trade a late failure for an early one
    that is sometimes wrong, and the service already survives an unreachable registry.
    """
    from robovast.execution.cluster_execution import cluster_image_build

    cfg, spec, registry = _submit_stubs(cs, monkeypatch)
    monkeypatch.setattr(cs, "_registry_has_image", lambda found: False)
    # `push_refused` collapses an unknown to False, which is what this asserts through.
    monkeypatch.setattr(cs._image_store, "push_refused", lambda image_ref: False,
                        raising=False)
    monkeypatch.setattr(cluster_image_build, "stage_context", lambda *a, **kw: 1234)

    class _Batch:
        def list_namespaced_job(self, namespace, label_selector=None):
            return types.SimpleNamespace(items=[])

        def create_namespaced_job(self, namespace, manifest):
            return None
    monkeypatch.setattr(cs, "_k8s_batch", lambda: _Batch())

    ref = cs._start_cluster_build(spec, "/proj", cfg, registry)
    assert cs.get_image_build_status(ref.build_id).phase == "building"


def test_a_submit_that_dies_before_its_job_exists_takes_its_context_with_it(
        cs, monkeypatch):
    """Otherwise the in-flight record that protects the context from the sweep would
    keep protecting it for the service's whole lifetime."""
    from robovast.execution.cluster_execution import cluster_image_build
    cfg, spec, registry = _submit_stubs(cs, monkeypatch)
    monkeypatch.setattr(cs, "_registry_has_image", lambda found: False)

    def stage(staged_dir, project_dir, dockerfile):
        staged_dir.mkdir(parents=True)
        (staged_dir / "Dockerfile").write_text(dockerfile)
        return 1
    monkeypatch.setattr(cluster_image_build, "stage_context", stage)

    class _Batch:
        def list_namespaced_job(self, namespace, label_selector=None):
            return types.SimpleNamespace(items=[])

        def create_namespaced_job(self, namespace, manifest):
            raise RuntimeError("the API server said no")
    monkeypatch.setattr(cs, "_k8s_batch", lambda: _Batch())

    with pytest.raises(RuntimeError, match="said no"):
        cs._start_cluster_build(spec, "/proj", cfg, registry)

    assert _staged_ids(cs) == set()
    # And the record does not hold the sweep back.
    assert cs.get_image_build_status("imgbuild-foo-h").done


def test_a_submit_is_refused_before_it_stages_anything(cs, monkeypatch):
    """A build that cannot happen must cost nothing, and must say why.

    The refusal has to come before staging -- that is a full copy of the project tree. It
    is also the only place this fault gets named: past here it surfaces as a gRPC dial
    error inside a build log, which reads as the project's own build configuration being
    wrong and sends whoever hit it to edit a `.vast` over a cluster fault.
    """
    from robovast.common.errors import ImageBuildFailed
    from robovast.execution.cluster_execution import cluster_image_build
    from robovast.execution.cluster_execution.buildkitd_deploy import BUILDKITD_NAME

    cfg, spec, registry = _submit_stubs(cs, monkeypatch)
    monkeypatch.setattr(cs, "_registry_has_image", lambda found: False)

    staged = []
    monkeypatch.setattr(cluster_image_build, "stage_context",
                        lambda *a, **kw: staged.append(True))
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.buildkitd_deploy.buildkitd_ready",
        lambda namespace: False)

    with pytest.raises(ImageBuildFailed) as excinfo:
        cs._start_cluster_build(spec, "/proj", cfg, registry)

    message = str(excinfo.value)
    assert BUILDKITD_NAME in message, "the refusal must name what is missing"
    assert "not a problem with this project" in message
    assert not staged, "nothing should be staged for a build that cannot run"
