# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The host <-> container protocol check: a supported window, not an equality.

It was `!=` at all three call sites, which meant the first COMPAT_VERSION bump orphaned every
image already published. A campaign whose results pin an image by digest could then never be
re-run -- not because the bytes were gone, but because the host refused to speak to them. Since
re-running a year-old campaign is a thing robovast exists to support, equality was refusing the
case the mechanism was built for.

So the host declares a range. The tests below pin the asymmetry that matters: an image OLDER
than the window is a "check out the revision the campaign recorded" problem, an image NEWER is
an "upgrade robovast" problem, and those are not interchangeable advice.
"""

import pytest

from robovast.common import execution as execution_mod
from robovast.common.execution import (COMPAT_VERSION, COMPAT_VERSION_LABEL, MIN_IMAGE_COMPAT,
                                       check_image_compat, image_compat_version)


@pytest.fixture(autouse=True)
def _no_remote_label_memo():
    """Reading a ref's labels is memoised per process, which would make these order-dependent.

    Cleared around every case rather than only before, so a test that populates it cannot reach
    the next one either way round.
    """
    execution_mod._REMOTE_LABEL_CACHE.clear()   # noqa: SLF001 - the memo is the thing under test
    yield
    execution_mod._REMOTE_LABEL_CACHE.clear()   # noqa: SLF001


def test_the_window_is_a_real_range_the_right_way_round():
    assert MIN_IMAGE_COMPAT <= COMPAT_VERSION


@pytest.mark.parametrize("version", range(MIN_IMAGE_COMPAT, COMPAT_VERSION + 1))
def test_everything_inside_the_window_is_accepted(version):
    assert check_image_compat("img:1", version=version, source="label") is None


def test_an_older_image_says_check_out_the_recorded_revision():
    """The advice equality gave was "pull the latest image", which is the *opposite* of what a
    re-run needs: it wants the bytes the campaign recorded, not today's. That mattered enough
    to assert on, because following the old message silently changes what ran."""
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


def test_a_local_image_is_read_without_touching_the_registry(monkeypatch):
    """The cheap path stays cheap: one `docker inspect`, no network, nothing started."""
    calls = []

    def fake_run(args, **_kwargs):
        calls.append(args[1] if len(args) > 1 else args)

        class Result:  # noqa: D401 - a stand-in for CompletedProcess
            returncode = 0
            stdout = "3"
        return Result()

    monkeypatch.setattr("subprocess.run", fake_run)
    version, source = image_compat_version("img:1")
    assert (version, source) == (3, "label")
    assert calls == ["inspect"], "nothing else may be asked once the local read answered"


def test_an_image_this_machine_does_not_have_is_read_from_the_registry(monkeypatch):
    """The case the label exists for, and the one it could not serve until now.

    A year-old campaign's image is not on the machine asking whether this host can still drive
    it. `docker inspect` fails there, and the answer is one config-blob read away -- so the
    pre-flight used to report "cannot tell" in precisely the situation it was built for.
    """
    import json as _json

    def fake_run(args, **_kwargs):
        class Result:
            returncode = 0 if "buildx" in args else 1
            stdout = _json.dumps(
                {"config": {"Labels": {"org.robovast.compat-version": "2"}}}
            ) if "buildx" in args else ""
        return Result()

    monkeypatch.setattr("subprocess.run", fake_run)
    assert image_compat_version("reg.example/img:1") == (2, "label")


def test_a_multi_arch_index_answers_from_any_platform(monkeypatch):
    """`imagetools` reports one config per platform for an index. They are pushed together, so
    any of them dates the protocol -- the same reasoning the registry client's index walk uses."""
    import json as _json

    def fake_run(args, **_kwargs):
        class Result:
            returncode = 0 if "buildx" in args else 1
            stdout = _json.dumps({
                "linux/amd64": {"config": {"Labels": {"org.robovast.compat-version": "2"}}},
                "linux/arm64": {"config": {"Labels": {"org.robovast.compat-version": "2"}}},
            }) if "buildx" in args else ""
        return Result()

    monkeypatch.setattr("subprocess.run", fake_run)
    assert image_compat_version("reg.example/img:1") == (2, "label")


def test_an_unlabelled_image_is_refused_rather_than_guessed(monkeypatch):
    """The cost of dropping the in-image file, asserted so it stays a known one.

    An image built before the label reports nothing at all now. That is the right answer --
    those images predate protocol 2, which is also the floor -- but the message has to say what
    to do about it rather than only that it could not tell.
    """
    def fake_run(args, **_kwargs):
        class Result:
            returncode = 0
            # `docker inspect` prints the Go zero value for a missing key, not "".
            stdout = "<no value>"
        return Result()

    monkeypatch.setattr("subprocess.run", fake_run)
    version, source = image_compat_version("img:1")
    assert version is None
    message = check_image_compat("img:1", version=version, source=source)
    assert "rebuild it from the revision the campaign recorded" in message


def test_no_docker_at_all_is_unknown_not_a_crash(monkeypatch):
    """A missing docker CLI must read as "cannot tell", not as an incompatible image -- the
    same confusion this codebase already fixed once for a missing docker reported as an
    unbuilt image."""
    def boom(*_args, **_kwargs):
        raise FileNotFoundError("docker")

    monkeypatch.setattr("subprocess.run", boom)
    version, source = image_compat_version("img:1")
    assert version is None
    assert "not reported" in source


def test_an_empty_image_reference_is_unknown():
    version, source = image_compat_version("")
    assert version is None
    assert "org.robovast.compat-version" in source


def test_the_label_name_is_namespaced():
    """OCI's own names are used where they exist; this one has no OCI equivalent, so it is
    namespaced rather than invented in the generic space."""
    assert COMPAT_VERSION_LABEL.startswith("org.robovast.")
