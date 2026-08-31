# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""A ref pinned before the pods existed IS the digest the runs used.

Image refs are resolved to digests *before* a batch's pods are written, so the kubelet gets
``IfNotPresent`` and every pod of a campaign provably runs the same bytes. The method that
records the digest into ``execution.yaml`` did not use that: it read the digest back off the
batch's pods afterwards, which is a race, and a short batch loses it.

A batch short enough to be reaped before the read loses it: ``image_revision`` is written
as ``unknown``, the search loop's per-batch bag conversion -- which resolves the campaign's
execution image from that file -- can then pick no image at all, and every batch fails to
score. The campaign reports that no run recorded a value, pointing at the world. A long
batch wins the race and is fine, which is what makes this intermittent by run count.

A digest ref cannot resolve to different bytes, so when the ref is already pinned there is
nothing to read back and no race to lose.
"""

PINNED = "harbor.example.com/robovast/robovast@sha256:" + "a" * 64
FLOATING = "harbor.example.com/robovast/robovast:latest"


def _runner(image):
    from robovast.execution.cluster_execution.kubernetes_backend import BatchJobRunner

    runner = BatchJobRunner.__new__(BatchJobRunner)
    runner.image = image
    runner.campaign = "camp"
    runner._resolved_image_digest = None
    runner._resolved_image_digests = None
    runner.namespace = "default"

    class _NoCluster:
        def list_namespaced_pod(self, *a, **kw):
            raise AssertionError("must not need the cluster when the ref is already pinned")

    runner.k8s_client = _NoCluster()
    return runner


def test_a_pinned_ref_is_recorded_without_reading_any_pod():
    runner = _runner(PINNED)
    runner._capture_image_digest("job-name=x")
    assert runner._resolved_image_digest == PINNED


def test_a_reaped_batch_no_longer_loses_the_digest():
    """The exact failure: pods gone, nothing to read, and the campaign still needs an image
    to convert its bags with."""
    runner = _runner(PINNED)

    class _Empty:
        def list_namespaced_pod(self, *a, **kw):
            return type("R", (), {"items": []})()

    runner.k8s_client = _Empty()
    runner._capture_image_digest("job-name=x")
    assert runner._resolved_image_digest == PINNED, (
        "a pinned ref must survive a batch whose pods are already gone")


def test_a_floating_ref_with_no_pods_stays_unresolved():
    """Unchanged for the case the read exists for: an unpinnable registry leaves the ref a
    tag, which is what would have run anyway. Recording a digest we never established
    would be a fabricated provenance claim."""
    runner = _runner(FLOATING)

    class _Empty:
        def list_namespaced_pod(self, *a, **kw):
            return type("R", (), {"items": []})()

    runner.k8s_client = _Empty()
    runner._capture_image_digest("job-name=x")
    assert runner._resolved_image_digest is None


def test_an_already_captured_digest_is_not_overwritten():
    """Search mode calls this once per batch; the first answer stands."""
    runner = _runner(PINNED)
    first = "harbor.example.com/robovast/robovast@sha256:" + "b" * 64
    runner._resolved_image_digest = first
    runner._capture_image_digest("job-name=x")
    assert runner._resolved_image_digest == first


def test_per_role_digests_are_still_read_when_the_pods_are_there():
    """The pod read is still worth doing -- it is the only source of a PER-CONTAINER digest,
    which a consumer needs to ask what image a particular role ran."""
    runner = _runner(PINNED)

    def _status(name, image_id):
        return type("CS", (), {"name": name, "image_id": image_id})()

    pod = type("P", (), {"status": type("S", (), {
        "container_statuses": [_status("sut", f"docker-pullable://{PINNED}")],
        "init_container_statuses": [],
    })()})()

    class _Pods:
        def list_namespaced_pod(self, *a, **kw):
            return type("R", (), {"items": [pod]})()

    runner.k8s_client = _Pods()
    runner._capture_image_digest("job-name=x")
    assert runner._resolved_image_digest == PINNED
    assert (runner._resolved_image_digests or {}).get("sut") == PINNED


# -- the per-container half, which the pod read alone could not deliver -------------

SIM_PINNED = "harbor.example.com/robovast/robovast-sim@sha256:" + "c" * 64


def _planned(**images):
    """A pinned container plan, as ``_pin_image_refs`` leaves it before pods are written."""
    containers = tuple(type("C", (), {"name": name, "image": image})()
                       for name, image in images.items())
    return type("Plan", (), {"containers": containers})()


def _empty_cluster(runner):
    class _Empty:
        def list_namespaced_pod(self, *a, **kw):
            return type("R", (), {"items": []})()

    runner.k8s_client = _Empty()
    return runner


def test_per_role_digests_come_from_the_pinned_plan_with_no_pods_at_all():
    """The resume case, and the one that produced this test.

    A resumed campaign's pods were reaped long before it started, so the pod read has nothing
    to say. The plan was pinned anyway. Recording only the campaign-level digest here is what
    left real campaigns with ``image_revision`` set and ``image_revisions`` absent -- and a
    retrigger then had no per-container ref to start from.
    """
    runner = _empty_cluster(_runner(PINNED))
    runner.plan = _planned(scenario=PINNED, simulation=SIM_PINNED)
    runner._capture_image_digest("job-name=x")
    assert runner._resolved_image_digests == {"scenario": PINNED, "simulation": SIM_PINNED}


def test_an_unreachable_cluster_still_records_the_planned_digests():
    """Best-effort applies to the pod read, not to what the plan already established."""
    runner = _runner(PINNED)
    runner.plan = _planned(scenario=PINNED)

    class _Down:
        def list_namespaced_pod(self, *a, **kw):
            raise RuntimeError("apiserver unreachable")

    runner.k8s_client = _Down()
    runner._capture_image_digest("job-name=x")
    assert runner._resolved_image_digests == {"scenario": PINNED}


def test_what_the_kubelet_pulled_wins_over_the_plan():
    """The pod read is the stronger claim where both speak: it is what actually ran."""
    actually_ran = "harbor.example.com/robovast/robovast@sha256:" + "e" * 64
    runner = _runner(PINNED)
    runner.plan = _planned(sut=PINNED)

    def _status(name, image_id):
        return type("CS", (), {"name": name, "image_id": image_id})()

    pod = type("P", (), {"status": type("S", (), {
        "container_statuses": [_status("sut", f"docker-pullable://{actually_ran}")],
        "init_container_statuses": [],
    })()})()

    class _Pods:
        def list_namespaced_pod(self, *a, **kw):
            return type("R", (), {"items": [pod]})()

    runner.k8s_client = _Pods()
    runner._capture_image_digest("job-name=x")
    assert runner._resolved_image_digests["sut"] == actually_ran


def test_a_later_batch_fills_a_per_role_digest_the_first_one_missed():
    """Guarding the early return on the campaign-level digest alone made this impossible:
    up-front pinning fills it on batch 1, so every later batch returned immediately."""
    runner = _empty_cluster(_runner(PINNED))
    runner.plan = _planned(scenario=PINNED)
    runner._resolved_image_digest = PINNED      # as batch 1 left it
    runner._resolved_image_digests = None       # ...with nothing per-role
    runner._capture_image_digest("job-name=x")
    assert runner._resolved_image_digests == {"scenario": PINNED}
