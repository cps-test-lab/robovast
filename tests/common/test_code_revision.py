# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Which revision is this process running — and saying so when it cannot be known.

A service loads robovast once at startup, so "is the change I just made loaded?" is a real
question, and the field that existed to answer it could not: `get_app_version` asks git from
the directory the module lives in, which in a deployed image is site-packages with no `.git`
above it, so the lookup always failed and the *package semver* was returned instead. `2.0.0`
looks like an answer, stays identical across every edit, and so silently made the staleness
check undetectable — the same shape as a missing docker CLI reported as an unbuilt image.

What is asserted here is therefore mostly about the empty string: it has to be reachable, and
it must not be papered over with a version.
"""

import pytest

from robovast.common.execution import GIT_REVISION_ENV, code_revision, get_app_version


def test_a_baked_revision_is_used_verbatim(monkeypatch):
    """The image path. It is checked FIRST because in a pod it is the only thing that can
    answer, and asking git there costs two failing subprocesses per call."""
    monkeypatch.setenv(GIT_REVISION_ENV, "abc1234")
    assert code_revision() == "abc1234"
    assert get_app_version() == "abc1234"


def test_a_dirty_marker_survives(monkeypatch):
    monkeypatch.setenv(GIT_REVISION_ENV, "abc1234+dirty")
    assert code_revision() == "abc1234+dirty"


def test_whitespace_only_is_not_a_revision(monkeypatch):
    """An unset `--build-arg` arrives as an empty ENV, not an absent one, so the empty case
    has to be recognised rather than returned as a revision that reads as present."""
    monkeypatch.setenv(GIT_REVISION_ENV, "   ")
    monkeypatch.setattr("robovast.common.execution._git_revision", lambda: None)
    assert code_revision() == ""


def test_no_revision_is_empty_and_never_the_package_version(monkeypatch):
    """The load-bearing one. An image with no baked revision and no repo must report "" —
    NOT `2.0.0`, which is what made this field unable to do its job."""
    monkeypatch.setenv(GIT_REVISION_ENV, "")
    monkeypatch.setattr("robovast.common.execution._git_revision", lambda: None)
    assert code_revision() == ""
    # And the version string still answers, so the compatibility handshake keeps working.
    assert get_app_version() and get_app_version() != ""


def test_the_version_still_falls_back_to_metadata(monkeypatch):
    """`get_app_version` may substitute the package version — it is a *version*. Only
    `code_revision` must refuse to, because a caller uses it to detect staleness."""
    monkeypatch.setenv(GIT_REVISION_ENV, "")
    monkeypatch.setattr("robovast.common.execution._git_revision", lambda: None)
    from importlib.metadata import version as pkg_version
    assert get_app_version() == pkg_version("robovast")
    assert code_revision() == "", "the two must not collapse into one answer"


def _checkout_revision():
    from robovast.common.execution import _git_revision
    return _git_revision() or ""


def test_a_source_checkout_reports_its_own_revision(monkeypatch):
    """Belt and braces: with nothing baked, a checkout still answers, so a developer running
    from source keeps the behaviour that already worked."""
    monkeypatch.delenv(GIT_REVISION_ENV, raising=False)
    revision = code_revision()
    if revision == "":
        pytest.skip("not a git checkout (installed copy) — nothing to compare against")
    assert revision == _checkout_revision()
