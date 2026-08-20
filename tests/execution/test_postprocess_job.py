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
                        lambda cfg, cid, cr: synced.append(cr) or 1)


def test_postprocess_syncs_outputs_even_on_conversion_failure(monkeypatch, tmp_path):
    """A failed conversion Job must still sync its teed postprocessing.log.

    The Job mirrors that log (with the conversion error) to the object store even
    on failure; syncing it lands the POSTPROCESSING section in the campaign log the
    web UI shows — otherwise the failure is only a terse "kubectl logs" hint.
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
