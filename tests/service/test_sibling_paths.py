# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A containerised service must hand the host daemon paths the host resolves the same way.

The failure this guards against does not raise on its own, which is the whole reason it is
worth a check: the daemon creates an empty directory at a path it cannot find and bind
mounts *that*, so the campaign runs, writes into a directory nobody will look in, and
reports success. Hours to diagnose, and nothing in the output names the cause.
"""

import pytest

from robovast.service.sibling_paths import (IDENTITY_MOUNTS_ENV, IN_CONTAINER_ENV,
                                            in_sibling_container, require_identity_mapped)


@pytest.fixture
def sibling(monkeypatch, tmp_path):
    """A service running as a sibling container, with *tmp_path* identity-mounted."""
    monkeypatch.setenv(IN_CONTAINER_ENV, "1")
    monkeypatch.setenv(IDENTITY_MOUNTS_ENV, str(tmp_path))
    return tmp_path


def test_a_host_service_checks_nothing(monkeypatch, tmp_path):
    """Not in a container: every path already means one thing, so this must not fire.

    Declared rather than sniffed. ``/.dockerenv`` would answer "am I in a container", which
    is the wrong question -- an in-pod cluster service is in one too and binds nothing.
    """
    monkeypatch.delenv(IN_CONTAINER_ENV, raising=False)
    assert in_sibling_container() is False
    require_identity_mapped(tmp_path / "anywhere", what="the results directory")


def test_a_path_under_an_identity_mount_is_accepted(sibling):
    require_identity_mapped(sibling / "results" / "camp-1", what="the results directory")


def test_the_mount_itself_is_accepted(sibling):
    require_identity_mapped(sibling, what="the results directory")


def test_a_path_outside_every_mount_is_refused(sibling, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    with pytest.raises(ValueError) as excinfo:
        require_identity_mapped(outside, what="the results directory")
    message = str(excinfo.value)
    # The path, the remedy, and what happens if it is ignored -- a refusal that only said
    # "invalid path" would leave the caller exactly where the silent failure does.
    assert str(outside) in message
    assert f"-v {outside}:{outside}" in message
    assert IDENTITY_MOUNTS_ENV in message
    assert "produces nothing" in message


def test_declaring_no_mounts_refuses_and_says_so(monkeypatch, tmp_path):
    monkeypatch.setenv(IN_CONTAINER_ENV, "1")
    monkeypatch.delenv(IDENTITY_MOUNTS_ENV, raising=False)
    with pytest.raises(ValueError, match="none declared"):
        require_identity_mapped(tmp_path, what="the results directory")


def test_several_mounts_are_each_honoured(monkeypatch, tmp_path_factory):
    first = tmp_path_factory.mktemp("results")
    second = tmp_path_factory.mktemp("workspaces")
    monkeypatch.setenv(IN_CONTAINER_ENV, "1")
    monkeypatch.setenv(IDENTITY_MOUNTS_ENV, f"{first}:{second}")
    require_identity_mapped(first / "a", what="x")
    require_identity_mapped(second / "b", what="x")
