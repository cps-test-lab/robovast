# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for analysis postprocessing surfacing."""

import logging

import robovast.execution.cluster_execution.postprocess_job as pj


def _patch_failed_conversion(monkeypatch, synced):
    monkeypatch.setattr(pj, "campaign_vast", lambda cr: "/x.vast")
    monkeypatch.setattr(pj, "rosbag_commands_for",
                        lambda vast, skip=None, skip_rosout=False: [{"plugins": []}])
    monkeypatch.setattr(pj, "campaign_execution_image", lambda cr: "img")
    monkeypatch.setattr(pj, "run_conversion_job", lambda *a, **k: (False, "boom"))
    monkeypatch.setattr(pj, "sync_outputs",
                        lambda cfg, cid, cr, force=False: synced.append(cr) or 1)


def test_postprocess_syncs_outputs_even_on_conversion_failure(monkeypatch, tmp_path):
    """A failed conversion Job must still sync its teed postprocessing.log.

    The Job mirrors that log (with the conversion error) to the object store even
    on failure; syncing it lands the POSTPROCESSING section in the campaign log the
    web UI shows — which is the only place the error is readable, since the Job is
    reaped by ttlSecondsAfterFinished minutes after it fails.
    """
    synced = []
    _patch_failed_conversion(monkeypatch, synced)

    ok, msg = pj.postprocess_campaign(object(), "camp", str(tmp_path), "ns")

    assert ok is False and msg == "boom"
    assert synced == [str(tmp_path)]  # synced despite the failure


def test_postprocess_echoes_conversion_log_to_console_on_failure(
        monkeypatch, tmp_path, caplog):
    """The synced postprocessing.log is also echoed to the service console."""
    _patch_failed_conversion(monkeypatch, [])
    exec_dir = tmp_path / "_execution"
    exec_dir.mkdir()
    (exec_dir / "postprocessing.log").write_text(
        "rosbags_process.py: error: unrecognized arguments: --output-root\n")

    with caplog.at_level(logging.WARNING, logger=pj.logger.name):
        pj.postprocess_campaign(object(), "camp", str(tmp_path), "ns")

    assert any("unrecognized arguments: --output-root" in r.message
               for r in caplog.records)


# ---------------------------------------------------------------------------
# a pod that cannot start
# ---------------------------------------------------------------------------

_CMDS = [{"plugins": [{"rosbags_tf_to_csv": {"frames": "all"}}], "bag_dir": "rosbag2"}]
_S3 = ("http://s3:9000", "ak", "sk", "bucket", "camp/")


def test_the_conversion_pod_can_pull_its_own_images():
    """The Job runs the campaign's execution image and mirrors bags with the sidecar, both of
    which may sit in a registry the kubelet has no credential for. Without this the pod stays
    ImagePullBackOff while the Job stays `active`, so the waiter reported a timeout and named
    neither image nor registry -- the same omission, in the same direction, that the build Job
    had."""
    m = pj.build_manifest("c1", "img:1", _CMDS, _S3, "ns", pull_secret_name="robovast-registry")
    assert m["spec"]["template"]["spec"]["imagePullSecrets"] == [{"name": "robovast-registry"}]


def test_no_credential_configured_leaves_the_pod_spec_alone():
    """A public-registry deployment needs none, and naming an absent Secret would itself keep
    the pod from starting."""
    m = pj.build_manifest("c1", "img:1", _CMDS, _S3, "ns")
    assert "imagePullSecrets" not in m["spec"]["template"]["spec"]


def test_a_failed_job_is_reported_without_a_cluster_command():
    """This message lands on ``postprocessing_error``, which the web UI renders to
    someone who has a log panel and no kubeconfig. An appended
    ``kubectl logs job/<name> -n <ns>`` is unrunnable for that reader, aimed at whichever
    cluster their context happens to name, and points at a Job that
    ``ttlSecondsAfterFinished`` reaps 300 s after it fails. The campaign log is where the
    conversion output actually is, and every surface already shows it.
    """
    msg = pj.job_failed_message("robovast-postproc-c1")

    assert "robovast-postproc-c1" in msg
    assert "POSTPROCESSING section of the campaign log" in msg
    # No cluster command, in any form: no tool name, no shell.
    assert "kubectl" not in msg and "`" not in msg and "$" not in msg


def test_a_blocked_pod_is_reported_by_its_reason(monkeypatch):
    """What the Job's own status cannot say: `active` is indistinguishable between "converting"
    and "will never start"."""
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.cluster_execution.blocked_job_reasons",
        lambda *_a, **_kw: {"job-x": "ImagePullBackOff: pull access denied"})
    assert pj._blocked_reason(object(), "ns", "job-x") == (
        "ImagePullBackOff: pull access denied")


def test_an_unreadable_pod_list_does_not_condemn_a_running_conversion(monkeypatch):
    """Advisory only: this check exists to sharpen a failure, so failing to make it must not
    turn a conversion that is working into a reported error."""
    def _boom(*_a, **_kw):
        raise RuntimeError("pods forbidden")

    monkeypatch.setattr(
        "robovast.execution.cluster_execution.cluster_execution.blocked_job_reasons", _boom)
    assert pj._blocked_reason(object(), "ns", "job-x") == ""


# -- a forced re-postprocess must force the download too ---------------------

def test_a_forced_repostprocess_forces_the_download(monkeypatch, tmp_path):
    """`force` means "bypass the per-rosbag caches and reconvert", so the Job REPLACES
    CSVs in the object store -- mutating objects in place.

    `download_prefix` skips a local file whose size already matches, which is right for
    the immutable durable home and wrong here: a regenerated CSV that happens to keep its
    byte count (a changed value of the same width, a re-derived column) is then skipped,
    and the campaign root keeps the file the user asked to replace. The re-run reports
    success and changes nothing that anyone can see, which is the worst shape a bug can
    take. `download_prefix`'s own docstring names this case as what `force=True` is for.
    """
    seen = {}
    monkeypatch.setattr(pj, "campaign_vast", lambda cr: "/x.vast")
    monkeypatch.setattr(pj, "rosbag_commands_for",
                        lambda vast, skip=None, skip_rosout=False: [{"plugins": []}])
    monkeypatch.setattr(pj, "campaign_execution_image", lambda cr: "img")
    monkeypatch.setattr(pj, "run_conversion_job", lambda *a, **k: (False, "stop here"))
    monkeypatch.setattr(pj, "sync_outputs",
                        lambda cfg, cid, cr, force=False: seen.setdefault("force", force))

    pj.postprocess_campaign(object(), "camp", str(tmp_path), "ns", force=True)
    assert seen["force"] is True, "a forced reconversion synced without forcing the fetch"


def test_an_ordinary_postprocess_leaves_the_skip_in_place(monkeypatch, tmp_path):
    """The default stays skip-existing: a first postprocess, and the search loop's
    per-batch sync, both re-list a prefix that only grows, and re-downloading every
    earlier batch's CSVs each time would add latency to a loop already bounded by a
    no-progress deadline."""
    seen = {}
    monkeypatch.setattr(pj, "campaign_vast", lambda cr: "/x.vast")
    monkeypatch.setattr(pj, "rosbag_commands_for",
                        lambda vast, skip=None, skip_rosout=False: [{"plugins": []}])
    monkeypatch.setattr(pj, "campaign_execution_image", lambda cr: "img")
    monkeypatch.setattr(pj, "run_conversion_job", lambda *a, **k: (False, "stop here"))
    monkeypatch.setattr(pj, "sync_outputs",
                        lambda cfg, cid, cr, force=False: seen.setdefault("force", force))

    pj.postprocess_campaign(object(), "camp", str(tmp_path), "ns")
    assert seen["force"] is False


def test_sync_outputs_passes_force_through_to_the_download(monkeypatch, tmp_path):
    """The parameter has to reach `download_prefix`, not just be accepted.

    Patched on the real module rather than substituted in ``sys.modules``: ``from . import
    in_pod_storage`` resolves through the parent package's attribute, so a sys.modules
    entry only wins while nothing has imported it yet -- which made an earlier version of
    this test pass alone and fail in the suite.
    """
    from robovast.execution.cluster_execution import in_pod_storage

    calls = {}

    class _Storage:
        def download_prefix(self, bucket, prefix, local_dir, force=False):
            calls['force'] = force
            return 3

    monkeypatch.setattr(in_pod_storage, 'campaign_storage_location',
                        lambda cfg, cid: ('b', 'p/'))
    monkeypatch.setattr(in_pod_storage, 'storage_client_for', lambda cfg: _Storage())

    assert pj.sync_outputs(object(), 'camp', str(tmp_path), force=True) == 3
    assert calls['force'] is True


# -- one campaign, many conversions ------------------------------------------

def test_two_conversions_of_one_campaign_get_different_job_names():
    """A search converts once per batch, and the Job name was the campaign's alone.

    Measured: batch 0's Job ran 16s and synced 146 outputs; batch 1's was "created" and
    "succeeded" in the SAME second and synced 0. `create_namespaced_job` returned 409, the
    code fell through to wait on the existing Job -- which was batch 0's, already complete --
    read `succeeded` and reported "rosbag conversion complete" having converted nothing. The
    extractor then refused the batch for a missing clearance, naming the world.

    The 409 fallthrough is right for what it was written for: a one-shot campaign
    postprocess, retried, should wait on the in-flight Job rather than duplicate it. It is
    wrong as soon as one campaign converts more than once, so the Job's identity has to say
    WHICH conversion it is.
    """
    names = {pj.build_manifest("camp-2026-08-25-1234", "img", [{"plugins": []}],
                               ("e", "a", "s", "b", "p/"), "ns",
                               discriminator=d)["metadata"]["name"]
             for d in ("batch-0", "batch-1", "batch-2")}
    assert len(names) == 3, f"batches collided on one Job name: {names}"


def test_the_same_conversion_keeps_a_stable_name():
    """So a genuine retry of one conversion still waits on the in-flight Job instead of
    launching a second copy of it -- the behaviour the 409 fallthrough exists for."""
    def name(disc):
        return pj.build_manifest("camp-x", "img", [{"plugins": []}],
                                 ("e", "a", "s", "b", "p/"), "ns",
                                 discriminator=disc)["metadata"]["name"]
    assert name("batch-3") == name("batch-3")


def test_no_discriminator_leaves_the_campaign_level_name_unchanged():
    """The campaign-level path converts once and its Job name is part of what an operator
    looks for; nothing about it should move because a search needed more names."""
    plain = pj.build_manifest("camp-x", "img", [{"plugins": []}],
                              ("e", "a", "s", "b", "p/"), "ns")["metadata"]["name"]
    assert plain == "robovast-postproc-camp-x"


def test_long_campaign_ids_stay_within_the_label_limit_and_stay_distinct():
    """Kubernetes copies the name into the pod template's `job-name` label, capped at 63.
    Truncation must not be what makes two batches collide again -- the hash is taken over
    the discriminated identity for exactly that reason."""
    long_id = "nav-search-adaptive-reps-2026-08-25-13573569-with-a-long-suffix"
    names = set()
    for disc in ("batch-0", "batch-1", "batch-10", "batch-1-reps-3", "batch-1-reps-5"):
        n = pj.build_manifest(long_id, "img", [{"plugins": []}],
                              ("e", "a", "s", "b", "p/"), "ns",
                              discriminator=disc)["metadata"]["name"]
        assert len(n) <= 63, f"{n} is {len(n)} chars"
        names.add(n)
    assert len(names) == 5, f"truncation collapsed distinct conversions: {names}"


def test_the_scripts_configmap_is_discriminated_too(monkeypatch):
    """Each conversion deletes its scripts ConfigMap when it finishes. Sharing one name
    across conversions means a finishing batch can delete the ConfigMap another is
    mounting, which surfaces as a pod stuck in ContainerCreating rather than as a name
    clash."""
    a = pj._scripts_cm_name("camp-x", discriminator="batch-0")
    b = pj._scripts_cm_name("camp-x", discriminator="batch-1")
    assert a != b
    assert pj._scripts_cm_name("camp-x") == "robovast-postproc-scripts-camp-x"
