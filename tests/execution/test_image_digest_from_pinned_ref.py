# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""A ref pinned before the pods existed IS the digest the runs used.

Image refs are resolved to digests *before* a batch's pods are written, so the kubelet gets
``IfNotPresent`` and every pod of a campaign provably runs the same bytes. The method that
records the digest into ``execution.yaml`` did not use that: it read the digest back off the
batch's pods afterwards, which is a race, and a short batch loses it.

Measured: an adaptive-repetitions campaign whose first group was 8 one-rep runs had its pods
reaped before the read. ``image_revision`` was written as ``unknown``, and the search loop's
per-batch bag conversion -- which resolves the campaign's execution image from that file --
could then pick no image at all. Every batch failed to score, and the campaign died reporting
"no run recorded a clearance value", pointing at the world. The five campaigns in the same
wave whose first batch was 24 runs all won the race and were fine.

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
