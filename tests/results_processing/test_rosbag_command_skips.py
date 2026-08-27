# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The skip list has to reach the container, not just exist in the scanner."""

from robovast.common.campaign_data import RESERVED_CAMPAIGN_DIRS
from robovast.results_processing import postprocessing_plugins as pp


class _Stop(BaseException):
    """Not an Exception: the plugin catches those and turns them into a return value."""


def _command(tmp_path, monkeypatch):
    captured = {}

    def _popen(cmd, **kwargs):
        captured["cmd"] = cmd
        raise _Stop  # the command is the subject; running it would launch a container

    monkeypatch.setattr(pp.subprocess, "Popen", _popen)
    try:
        pp.RosbagsProcess()(str(tmp_path), str(tmp_path), [{"type": "x"}])
    except _Stop:
        pass
    return captured.get("cmd", [])


def test_every_reserved_directory_is_passed_to_the_scanner(tmp_path, monkeypatch):
    """rosbags_common cannot import the definition -- it is copied into the container
    standalone -- so the plugin is the only place the two can be kept in step."""
    cmd = _command(tmp_path, monkeypatch)
    skipped = {cmd[i + 1] for i, a in enumerate(cmd) if a == "--skip-dir"}
    assert skipped == set(RESERVED_CAMPAIGN_DIRS), (
        "a directory added to RESERVED_CAMPAIGN_DIRS must be skipped without a second edit")


def test_the_calibration_probes_are_among_them(tmp_path, monkeypatch):
    """The one that motivated this: a probe is not a run, and its unfinalized bag failed
    the whole postprocessing step on data nothing was going to read."""
    assert "_calibration" in RESERVED_CAMPAIGN_DIRS
    cmd = _command(tmp_path, monkeypatch)
    assert "_calibration" in cmd
