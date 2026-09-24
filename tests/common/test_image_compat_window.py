# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The host <-> container protocol check: a supported window, not an equality.

An image older than the window is fixed by the revision the campaign recorded, an image newer
by upgrading robovast; the advice is not interchangeable.
"""

import pytest

from robovast.common.execution import (COMPAT_VERSION, COMPAT_VERSION_LABEL, MIN_IMAGE_COMPAT,
                                       check_image_compat)


def test_the_window_is_a_real_range_the_right_way_round():
    assert MIN_IMAGE_COMPAT <= COMPAT_VERSION


@pytest.mark.parametrize("version", range(MIN_IMAGE_COMPAT, COMPAT_VERSION + 1))
def test_everything_inside_the_window_is_accepted(version):
    assert check_image_compat("img:1", version=version, source="label") is None


def test_an_older_image_says_check_out_the_recorded_revision():
    """A re-run needs the bytes the campaign recorded, so the advice is the recorded revision,
    never a newer image."""
    message = check_image_compat("img:1", version=MIN_IMAGE_COMPAT - 1, source="label")
    assert message is not None
    assert "robovast_revision" in message
    assert "Do NOT pull a newer image" in message


def test_a_newer_image_says_upgrade_robovast():
    """Opposite direction, opposite fix. Rebuilding the image here would be wrong -- the image
    is fine and this robovast is behind it."""
    message = check_image_compat("img:1", version=COMPAT_VERSION + 1, source="label")
    assert message is not None
    assert "upgrade robovast" in message
    assert "robovast_revision" not in message


def test_an_image_that_reports_nothing_is_refused_not_assumed():
    """Silence is not consent. An image with no marker is either not a robovast image or
    predates both markers, and guessing it is current would push the failure into the run."""
    message = check_image_compat("img:1", version=None, source="no marker")
    assert message is not None
    assert f"{MIN_IMAGE_COMPAT}..{COMPAT_VERSION}" in message


def test_the_label_name_is_namespaced():
    """OCI's own names are used where they exist; this one has no OCI equivalent, so it is
    namespaced rather than invented in the generic space."""
    assert COMPAT_VERSION_LABEL.startswith("org.robovast.")
