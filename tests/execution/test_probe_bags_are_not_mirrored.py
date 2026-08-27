# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The cluster lane keeps probe bags out by never fetching them.

``/bags`` is an ``mc mirror`` of the campaign prefix, not a bind mount, so "do not give the
Job the probe output" is an exclusion on that mirror. What the Job never receives it cannot
convert, cannot fail on, and does not pay to download.
"""

from robovast.common.campaign_data import PROBE_DIR
from robovast.execution.cluster_execution.postprocess_job import _mirror_excludes


def test_the_probe_directory_is_excluded_from_the_mirror():
    assert f"--exclude '{PROBE_DIR}/*'" in _mirror_excludes()


def test_nothing_else_is_excluded():
    """``_jobs`` holds every job's real ``logs/rosout_bag``; excluding the reserved set
    wholesale would drop the campaign's whole /rosout record while still exiting zero."""
    assert _mirror_excludes().count("--exclude") == 1
    assert "_jobs" not in _mirror_excludes()
