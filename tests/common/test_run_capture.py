# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""The capture format's version contract, on the side that reads world identity.

The properties here are the ones that fail *invisibly*. A capture this code cannot interpret still
parses -- every version of this format has had the same shape -- so nothing crashes; the geometry is
simply compiled from fields that no longer mean what they used to, and the result looks right.
"""

import pytest

from robovast.common import run_capture


def _manifest(**over):
    base = {"format": run_capture.FORMAT, "version": run_capture.FORMAT_VERSION}
    base.update(over)
    return base


def test_the_current_version_is_supported():
    assert run_capture.check_supported(_manifest()) == run_capture.FORMAT_VERSION


def test_the_version_roqsim_currently_produces_is_supported():
    """A LITERAL, unlike the case above, which passes at any value of ``FORMAT_VERSION``.

    That is the bug this module exists to prevent: roqsim shipped v2 captures against consumers
    stuck at v1, and nothing failed until somebody opened a 3D panel. A literal is what fails when
    the two drift again -- and editing it is exactly the moment to confirm this side was taught the
    new version rather than left behind by it.
    """
    assert run_capture.check_supported(_manifest(version=2)) == 2


def test_an_older_version_is_read_not_refused():
    """v1 captures replay: their motion is identical, and their overrides are resolved by the roqsim
    that wrote them, in the campaign's own pinned image. Supported, not merely tolerated."""
    assert run_capture.check_supported(_manifest(version=1)) == 1


def test_an_absent_version_is_the_oldest_format():
    manifest = _manifest()
    del manifest["version"]
    assert run_capture.manifest_version(manifest) == 1


def test_a_newer_version_is_refused_by_name_and_says_what_would_fix_it():
    """Both halves are the message's job. The number, so whoever reads it knows which version to
    teach this code; the pointer to the spec, because the fix is never the capture's."""
    newer = run_capture.FORMAT_VERSION + 1
    with pytest.raises(run_capture.CaptureFormatError) as err:
        run_capture.check_supported(_manifest(version=newer))
    assert str(newer) in str(err.value)
    assert "run_capture.rst" in str(err.value)


def test_a_version_that_is_not_a_number_is_refused_rather_than_coerced():
    with pytest.raises(run_capture.CaptureFormatError, match="not a number"):
        run_capture.manifest_version(_manifest(version="two"))


def test_nothing_is_read_out_of_an_empty_manifest():
    """A missing or unreadable capture reaches this as ``{}``; it must not look like a valid v0."""
    assert run_capture.manifest_version({}) == 1
    assert run_capture.manifest_version(None) == 1
