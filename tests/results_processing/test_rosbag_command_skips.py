# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The probe directory must be kept out of the bag scan -- and only that one."""

from robovast.common.campaign_data import PROBE_DIR
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


def _skipped(cmd):
    return {cmd[i + 1] for i, a in enumerate(cmd) if a == "--skip-dir"}


def test_the_probe_directory_is_kept_out_of_the_scan(tmp_path, monkeypatch):
    """A probe is deliberately not a run, so its bag is not campaign data -- and an
    interrupted probe's unfinalized bag failed the whole step on data nothing would read."""
    assert PROBE_DIR in _skipped(_command(tmp_path, monkeypatch))


def test_the_jobs_directory_is_never_skipped(tmp_path, monkeypatch):
    """The hazard that nearly shipped, and the reason this rule is not "skip every reserved
    directory". ``_jobs/<batch>/<job>/logs/rosout_bag`` is each job's REAL log bag, so
    skipping the reserved set wholesale would silently drop every /rosout record in the
    campaign -- a conversion that succeeds while producing nothing, which is worse than the
    failure it was meant to fix. The names look interchangeable and are not."""
    skipped = _skipped(_command(tmp_path, monkeypatch))
    assert "_jobs" not in skipped
    assert skipped == {PROBE_DIR}, "only the probe directory holds no campaign data"
