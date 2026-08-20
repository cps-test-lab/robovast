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
from robovast.execution.cluster_execution.image_warm import (WARM_DAEMONSET_NAME,
                                                             WARM_DEADLINE_SECONDS,
                                                             WARM_JOBGROUP,
                                                             WARM_SLEEP_SECONDS,
                                                             WARM_TTL_SECONDS,
                                                             warm_daemonset_manifest,
                                                             warm_id_for,
                                                             warm_job_manifest)
from robovast.service.interface import ImageBuildStatus
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore
from robovast.service.image_build import BuildSpec

BUILD = "imgbuild-sut-abc123"
REF = "harbor.example.de/robovast/sut:abc123"


class _Apps:
    """An apps API that records the DaemonSet it was asked to create or patch."""

    def __init__(self, exists=False, fail_with=None):
        self.created = []
        self.patched = []
        self.deleted = []
        self._exists = exists
        self._fail_with = fail_with

    def create_namespaced_daemon_set(self, namespace, manifest):
        if self._fail_with is not None:
            raise self._fail_with
        if self._exists:
            raise _api_exception(409)
        self.created.append((namespace, manifest))

    def replace_namespaced_daemon_set(self, name, namespace, manifest):
        self.patched.append((name, namespace, manifest))

    def delete_namespaced_daemon_set(self, name, namespace, body=None):
        self.deleted.append((name, namespace))

    @property
    def applied(self):
        """The manifest that was applied, whichever verb got it there."""
        if self.created:
            return self.created[0][1]
        return self.patched[0][2]

    @property
    def images(self):
        return [c["image"] for c in
                self.applied["spec"]["template"]["spec"]["containers"]]


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
    return BuildSpec(tag="sut", base_image="")


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


# ---------------------------------------------------------------------------
# the family images, at setup / upgrade
# ---------------------------------------------------------------------------

def test_the_family_set_warmed_is_the_three_a_campaign_runs(monkeypatch):
    """``robovast-controller`` must stay out: it *is* the service Deployment, which runs
    ``imagePullPolicy: Always``, so the kubelet pulls it during the rollout that
    setup/upgrade already performs. Warming it would duplicate that pull."""
    from robovast.common.execution import FAMILY_MEMBERS
    from robovast.execution.cluster_execution.image_warm import (WARM_FAMILY_MEMBERS,
                                                                 family_refs_to_warm)
    monkeypatch.setenv("ROBOVAST_PROJECT", "harbor.example.de/robovast")
    monkeypatch.setenv("ROBOVAST_PROJECT_TAG", "2026-08-20")

    assert set(WARM_FAMILY_MEMBERS) == set(FAMILY_MEMBERS) - {"robovast-controller"}
    assert family_refs_to_warm() == [
        "harbor.example.de/robovast/robovast:2026-08-20",
        "harbor.example.de/robovast/robovast-roqsim:2026-08-20",
        "harbor.example.de/robovast/robovast-sidecar:2026-08-20",
    ]


def test_the_family_refs_follow_the_environment_being_deployed(monkeypatch):
    """setup/upgrade bakes ROBOVAST_PROJECT into the service pod, so resolving from the same
    environment is what makes this warm the set the deployment is being pointed *at*."""
    from robovast.execution.cluster_execution.image_warm import family_refs_to_warm
    monkeypatch.setenv("ROBOVAST_PROJECT", "ghcr.io/other-ns")
    monkeypatch.delenv("ROBOVAST_PROJECT_TAG", raising=False)

    assert all(r.startswith("ghcr.io/other-ns/") and r.endswith(":latest")
               for r in family_refs_to_warm())


def test_an_unreachable_cluster_does_not_fail_a_finished_deployment(monkeypatch):
    """This runs *after* setup/upgrade has converged. It must not be able to undo that."""
    from robovast.execution.cluster_execution import image_warm
    monkeypatch.setattr(image_warm, "family_refs_to_warm",
                        lambda: ["harbor.example.de/robovast/robovast:latest"])
    monkeypatch.setattr("robovast.execution.cluster_execution.kube_client."
                        "load_kube_config",
                        lambda ctx=None: (_ for _ in ()).throw(RuntimeError("no cluster")))

    assert image_warm.warm_family_images("ns1", None) == []


def _submit_stubs(cs, monkeypatch, batch, base_image="",
                  deployment_base="harbor.example.de/robovast/robovast:t"):
    """Stub a submit far enough that it reaches Job creation, so the base fire point runs."""
    from robovast.execution.cluster_execution import cluster_image_build, in_pod_storage
    from robovast.service.image_store import ImageRef

    monkeypatch.setattr(in_pod_storage, "storage_client_for", lambda cfg: object())
    monkeypatch.setattr(cs, "_image_store", types.SimpleNamespace(
        ref_for=lambda spec_, dir_: ImageRef(ref=REF, identity="build:sut@abc123",
                                             build_id=BUILD, image_hash="abc123"),
        resolve_vcs=lambda spec_: {},
        git_secret_name=lambda: "",
        pull_secret_name=lambda: "reg-push"), raising=False)
    monkeypatch.setattr(cs, "_existing_build_job", lambda bid: None)
    monkeypatch.setattr(cs, "_sweep_build_contexts", lambda cfg, bucket: None)
    monkeypatch.setattr(cs, "_registry_has_image", lambda found: False)
    monkeypatch.setattr(cs, "_k8s_batch", lambda: batch)
    monkeypatch.setattr("robovast.service.image_build.generate_dockerfile",
                        lambda spec, project_dir, base_ref, resolved_vcs=None: "FROM base")
    # A ready build daemon: these tests are about what a submit does, and without one the
    # submit correctly refuses before it does any of it.
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.buildkitd_deploy.buildkitd_ready",
        lambda namespace: True)
    monkeypatch.setattr(cluster_image_build, "stage_context_to_s3",
                        lambda *a, **kw: None)
    monkeypatch.setattr(cluster_image_build, "build_job_manifest",
                        lambda **kw: {"metadata": {"name": kw["build_id"]},
                                      "spec": {"template": {"spec": {"containers": [
                                          {"image": kw["image_ref"]}]}}}})
    cfg = types.SimpleNamespace(get_s3_credentials=lambda: ("ak", "sk"),
                                get_s3_endpoint=lambda: "http://robovast:9000",
                                get_host_aliases=lambda: None)
    spec = BuildSpec(tag="sut", base_image=base_image)
    registry = types.SimpleNamespace(registry_prefix="harbor.example.de/robovast",
                                     push_secret_name="push", pull_secret_name="reg-push",
                                     insecure=False, ca_configmap_name="",
                                     base_experiment_image=deployment_base)
    return cfg, spec, registry


def test_a_submit_warms_the_resolved_base_alongside_the_build(cs, monkeypatch):
    """The base is most of the built image, and a build takes minutes -- so this pull runs
    concurrently with it and is off the critical path entirely. Asserted on the *resolved*
    base, because `spec.base_image` is often empty and the default supplied it."""
    batch = _Batch()
    cfg, spec, registry = _submit_stubs(cs, monkeypatch, batch)

    ref = cs._start_cluster_build(spec, "/proj", cfg, registry, "bkt")
    assert ref.cached is False
    # The deployment default, since this spec declares no base of its own -- the common
    # case, and the one a test asserting `spec.base_image` would silently miss.
    base = "harbor.example.de/robovast/robovast:t"
    # The build Job, then the prewarm: the prewarm must not replace or precede it.
    assert batch.names == [BUILD, warm_id_for(base)]
    assert batch.images[1] == base


def test_a_submit_whose_base_prewarm_fails_still_submits_the_build(cs, monkeypatch):
    """The build is the thing that was asked for; the prewarm is not."""
    class _OnlyBuildWorks(_Batch):
        def create_namespaced_job(self, namespace, manifest):
            if manifest["metadata"]["name"].startswith("imgwarm-"):
                raise _api_exception(403)
            super().create_namespaced_job(namespace, manifest)

    batch = _OnlyBuildWorks()
    cfg, spec, registry = _submit_stubs(cs, monkeypatch, batch,
                                        base_image="harbor.example.de/robovast/robovast:t")

    assert cs._start_cluster_build(spec, "/proj", cfg, registry, "bkt").cached is False
    assert batch.names == [BUILD]


def test_the_warm_pods_pull_rather_than_trusting_what_the_node_has(monkeypatch):
    """The whole point of the DaemonSet. A floating tag is never re-pulled under
    ``IfNotPresent`` once a node holds bytes for it, and every campaign pod runs
    ``IfNotPresent`` so a sweep does not depend on the registry -- which leaves this as the
    only place a re-pushed ``:latest`` can reach a node at all."""
    manifest = warm_daemonset_manifest(image_refs=["r/a:latest", "r/b:latest"], namespace="ns1")
    containers = manifest["spec"]["template"]["spec"]["containers"]

    assert [c["imagePullPolicy"] for c in containers] == ["Always", "Always"]
    # And the Job path too: it is the same argument, and it never fails its caller either.
    assert (warm_job_manifest(image_ref=REF, namespace="ns1")["spec"]["template"]["spec"]
            ["containers"][0]["imagePullPolicy"]) == "Always"


def test_a_floating_tag_still_rolls_the_daemonset(monkeypatch):
    """Without the restart annotation this mechanism silently does nothing in exactly the case
    it exists for: a re-pushed ``:latest`` leaves every field byte-identical, so a patch would
    change nothing, roll no pod, and re-pull nothing -- the trap ``service_deploy`` documents.
    ``Always`` only helps a container that *starts*."""
    from robovast.execution.cluster_execution.service_deploy import RESTART_ANNOTATION
    first = warm_daemonset_manifest(image_refs=["r/a:latest"], namespace="ns1", stamp="t1")
    second = warm_daemonset_manifest(image_refs=["r/a:latest"], namespace="ns1", stamp="t2")

    annotations = first["spec"]["template"]["metadata"]["annotations"]
    assert annotations[RESTART_ANNOTATION] == "t1"
    assert first["spec"]["template"] != second["spec"]["template"]


def test_every_image_gets_a_sleeping_container_so_the_kubelet_cannot_collect_it(monkeypatch):
    """An exited container's image is collectable again, so init containers would warm the node
    and then let it go cold. A running container is what pins the bytes -- which is the one
    property the Job shape cannot have."""
    refs = ["r/a:t", "r/b:t", "r/c:t"]
    containers = (warm_daemonset_manifest(image_refs=refs, namespace="ns1")
                  ["spec"]["template"]["spec"]["containers"])

    assert [c["image"] for c in containers] == refs
    assert all(c["command"] == ["sleep", str(WARM_SLEEP_SECONDS)] for c in containers)
    # A plain integer, not `sleep infinity`: that is a GNU extension and robovast-sidecar is
    # alpine, where busybox rejects it and the pod would crash-loop on every node.
    assert str(WARM_SLEEP_SECONDS).isdigit()


def test_the_warm_pods_tolerate_what_campaign_pods_tolerate(monkeypatch):
    """A warm pod that does not tolerate the campaign nodes' taint skips precisely the nodes
    worth warming -- and reports success while doing it. Read from where the ResourceFlavor
    granting it is written, so the two cannot drift."""
    from robovast.execution.cluster_execution.kubernetes_kueue import KUEUE_JOB_TOLERATIONS
    spec = warm_daemonset_manifest(image_refs=["r/a:t"], namespace="ns1")["spec"]["template"]["spec"]

    assert spec["tolerations"] == [dict(t) for t in KUEUE_JOB_TOLERATIONS]
    # No nodeSelector: missing a node that runs a cell is the failure that matters, while an
    # extra warmed node costs one pull nobody reads.
    assert "nodeSelector" not in spec


def test_warming_the_family_declares_one_daemonset_with_the_pull_secret(monkeypatch):
    from robovast.execution.cluster_execution import image_warm
    refs = ["harbor.example.de/robovast/robovast:t",
            "harbor.example.de/robovast/robovast-roqsim:t",
            "harbor.example.de/robovast/robovast-sidecar:t"]
    apps = _Apps()
    monkeypatch.setattr(image_warm, "family_refs_to_warm", lambda: refs)
    monkeypatch.setattr("robovast.execution.cluster_execution.kube_client."
                        "load_kube_config", lambda ctx=None: None)
    monkeypatch.setattr("kubernetes.client.AppsV1Api", lambda: apps)
    monkeypatch.setattr("kubernetes.client.CoreV1Api", lambda: types.SimpleNamespace(
        read_namespaced_secret=lambda name, ns: object()))

    assert image_warm.warm_family_images("ns1", None) == refs
    assert apps.images == refs
    assert (apps.applied["spec"]["template"]["spec"]["imagePullSecrets"]
            == [{"name": "robovast-registry-push"}])


def test_an_upgrade_refreshes_the_existing_daemonset_instead_of_standing_up_a_second(monkeypatch):
    """Idempotency here is the fixed name, not a hash of the refs: one object per deployment,
    updated in place so the controller rolls the pods itself and the nodes are never left with
    nothing warm in between."""
    from robovast.execution.cluster_execution import image_warm
    apps = _Apps(exists=True)
    monkeypatch.setattr(image_warm, "family_refs_to_warm", lambda: ["ghcr.io/x/robovast:1"])
    monkeypatch.setattr("robovast.execution.cluster_execution.kube_client."
                        "load_kube_config", lambda ctx=None: None)
    monkeypatch.setattr("kubernetes.client.AppsV1Api", lambda: apps)
    monkeypatch.setattr("kubernetes.client.CoreV1Api", lambda: types.SimpleNamespace(
        read_namespaced_secret=lambda name, ns: object()))

    assert image_warm.warm_family_images("ns1", None) == ["ghcr.io/x/robovast:1"]
    assert apps.created == []
    assert [name for name, _ns, _m in apps.patched] == [WARM_DAEMONSET_NAME]


def test_a_public_registry_warms_without_a_credential(monkeypatch):
    """Naming a Secret that does not exist keeps the pod from starting, so an absent one
    must mean "warm without a credential", not "do not warm"."""
    from robovast.execution.cluster_execution import image_warm
    apps = _Apps()
    monkeypatch.setattr(image_warm, "family_refs_to_warm", lambda: ["ghcr.io/x/robovast:1"])
    monkeypatch.setattr("robovast.execution.cluster_execution.kube_client."
                        "load_kube_config", lambda ctx=None: None)
    monkeypatch.setattr("kubernetes.client.AppsV1Api", lambda: apps)
    monkeypatch.setattr("kubernetes.client.CoreV1Api", lambda: types.SimpleNamespace(
        read_namespaced_secret=lambda name, ns: (_ for _ in ()).throw(
            _api_exception(404))))

    assert image_warm.warm_family_images("ns1", None) == ["ghcr.io/x/robovast:1"]
    assert "imagePullSecrets" not in apps.applied["spec"]["template"]["spec"]


def test_a_daemonset_that_cannot_be_declared_does_not_fail_the_deployment(monkeypatch):
    """One object means no partial outcome to report -- the trade for covering every node. It
    is bounded by this still never being able to fail an upgrade that has already converged,
    and by the return value saying nothing was covered rather than overstating it."""
    from robovast.execution.cluster_execution import image_warm
    apps = _Apps(fail_with=_api_exception(403))
    monkeypatch.setattr(image_warm, "family_refs_to_warm", lambda: ["ghcr.io/x/robovast:1"])
    monkeypatch.setattr("robovast.execution.cluster_execution.kube_client."
                        "load_kube_config", lambda ctx=None: None)
    monkeypatch.setattr("kubernetes.client.AppsV1Api", lambda: apps)
    monkeypatch.setattr("kubernetes.client.CoreV1Api", lambda: types.SimpleNamespace(
        read_namespaced_secret=lambda name, ns: object()))

    assert image_warm.warm_family_images("ns1", None) == []


def test_an_empty_family_is_an_error_rather_than_an_empty_daemonset(monkeypatch):
    """A family that failed to resolve must not become a DaemonSet the API server rejects for
    an unrelated-sounding reason."""
    with pytest.raises(ValueError):
        warm_daemonset_manifest(image_refs=[], namespace="ns1")


def test_teardown_removes_the_daemonset(monkeypatch):
    """Teardown deletes named objects rather than the namespace, so without this the warm pods
    outlive the deployment -- on every node, indefinitely."""
    from robovast.execution.cluster_execution import image_warm
    apps = _Apps()
    monkeypatch.setattr("robovast.execution.cluster_execution.kube_client."
                        "load_kube_config", lambda ctx=None: None)
    monkeypatch.setattr("kubernetes.client.AppsV1Api", lambda: apps)
    monkeypatch.setattr("kubernetes.client.V1DeleteOptions",
                        lambda **kw: kw, raising=False)

    assert image_warm.delete_warm_daemonset("ns1", None) is True
    assert apps.deleted == [(WARM_DAEMONSET_NAME, "ns1")]


def test_a_tag_bump_replaces_the_containers_instead_of_piling_them_up(monkeypatch):
    """A container name is a merge key. Carrying the tag in it would make a tag bump look like
    a different container, so the family would accumulate one entry per tag ever deployed."""
    before = warm_daemonset_manifest(image_refs=["r/robovast:1", "r/robovast-roqsim:1"],
                                     namespace="ns1")
    after = warm_daemonset_manifest(image_refs=["r/robovast:2", "r/robovast-roqsim:2"],
                                    namespace="ns1")

    names = lambda m: [c["name"] for c in m["spec"]["template"]["spec"]["containers"]]
    assert names(before) == names(after)
    assert len(set(names(before))) == 2
