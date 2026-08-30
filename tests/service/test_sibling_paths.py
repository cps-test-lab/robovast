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


# --- the wiring, not the helper ---------------------------------------------
#
# Every case above proves the check *can* refuse. What decides whether an operator ever
# sees one is which paths ``vast serve`` asks about, and that list is the part with no
# natural reminder: a path is added where it gets *used*, not where it is checked. The
# workspaces store was already missing from it -- startup succeeded and the first upload
# 500'd on a mkdir into a container-only path, naming the symptom and not the cause.


@pytest.fixture
def serve_cli(monkeypatch, tmp_path):
    """``vast serve --backend local`` as a sibling, stopped where the checks end."""
    import tempfile

    from click.testing import CliRunner

    from robovast.common.cli import core_commands

    monkeypatch.setenv(IN_CONTAINER_ENV, "1")
    monkeypatch.setenv(IDENTITY_MOUNTS_ENV, str(tmp_path))
    # It has to exist: gettempdir() silently falls back to /tmp for a TMPDIR it cannot
    # write into, which is the same thing an operator sees for a directory the compose
    # file names but nobody created.
    (tmp_path / "tmp").mkdir()
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    monkeypatch.setenv("ROBOVAST_WORKSPACES_ROOT", str(tmp_path / "workspaces"))
    # gettempdir() caches into this on first use, and pytest has used it long before now.
    monkeypatch.setattr(tempfile, "tempdir", None)
    # The SPA build runs first and wants npm; it is not what is under test.
    monkeypatch.setattr(core_commands, "_ensure_ui_built", lambda rebuild=False: None)
    monkeypatch.setattr("robovast.service.serve_backends.resolve",
                        lambda name: _StopAtTheEndOfTheChecks())

    def run(*args):
        return CliRunner().invoke(core_commands.serve, ["--backend", "local", *args])
    return run


class _StopAtTheEndOfTheChecks:
    """A lane that ends the run where the checks do: past them nothing is under test."""

    storage = "local filesystem"

    def build(self, **kwargs):
        del kwargs
        raise SystemExit(0)


@pytest.mark.parametrize("named", ["--results-dir", "ROBOVAST_WORKSPACES_ROOT", "TMPDIR"])
def test_serve_checks_every_path_it_hands_the_daemon(serve_cli, monkeypatch, tmp_path,
                                                     tmp_path_factory, named):
    outside = tmp_path_factory.mktemp("outside")
    args = []
    if named == "--results-dir":
        args = [named, str(outside)]
    else:
        monkeypatch.setenv(named, str(outside))
        # The default results root is derived from the workspaces store, so moving that
        # out moves both; name one inside the mount so this asserts about the store.
        args = ["--results-dir", str(tmp_path / "results")]

    result = serve_cli(*args)

    assert result.exit_code != 0, f"{named} reaches the daemon unchecked"
    assert named in result.output, "the refusal has to name the input the operator sets"
    assert str(outside) in result.output


def test_serve_starts_when_every_path_is_mapped(serve_cli):
    """The other half: a correctly mounted service must not be refused."""
    result = serve_cli()
    assert result.exit_code == 0, result.output
