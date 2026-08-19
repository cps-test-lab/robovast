# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Making ``vast`` reachable from a shell nobody activated a venv in.

An agent's shell is not the one you typed ``vast login`` into: it is started from your
profile, with no venv active. Storing credentials it can use while leaving the command it
must run unreachable gets it exactly halfway — and the failure surfaces as
``command not found`` in the middle of something, not at setup time.

A venv console script's shebang is an absolute interpreter path, so a symlink to it runs
with nothing activated. The only thing a later shell must supply is the link's directory
on PATH, which is why the target is chosen from a **login shell's** PATH rather than this
process's: they differ precisely because a venv is active here and nowhere else, so
linking into the venv's own bin would look like success and change nothing.
"""

import os
from pathlib import Path

import pytest

from robovast.client import login as login_config


@pytest.fixture
def fake_env(tmp_path, monkeypatch):
    """A scratch HOME, a fake console script, and a controllable login-shell PATH."""
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

    script = tmp_path / "venv" / "bin" / "vast"
    script.parent.mkdir(parents=True)
    script.write_text("#!/usr/bin/python3\n")
    script.chmod(0o755)
    monkeypatch.setattr(login_config, "cli_path", lambda: script)
    return home, script


def _path_is(monkeypatch, *dirs):
    monkeypatch.setattr(login_config, "_path_dirs", lambda: list(dirs))


def _resolves(monkeypatch, ok: bool):
    """Stand in for the `bash -lc 'command -v vast'` confirmation."""
    import subprocess

    real = subprocess.run

    def fake(cmd, *a, **k):
        if cmd[:2] == ["bash", "-lc"] and "command -v vast" in cmd[2]:
            return subprocess.CompletedProcess(cmd, 0 if ok else 1, "", "")
        # passthrough; the caller owns check=
        return real(cmd, *a, **k)  # pylint: disable=subprocess-run-check

    monkeypatch.setattr(subprocess, "run", fake)


def test_it_links_into_local_bin_when_that_is_on_the_path(fake_env, monkeypatch):
    home, script = fake_env
    _path_is(monkeypatch, home / ".local" / "bin", Path("/usr/bin"))
    _resolves(monkeypatch, True)

    linked, message = login_config.link_cli()
    link = home / ".local" / "bin" / "vast"
    assert linked, message
    assert link.is_symlink() and link.resolve() == script


def test_it_never_links_into_a_directory_off_the_login_path(fake_env, monkeypatch):
    """The venv's own bin is on *this* PATH and no other — linking there is a no-op
    dressed as success, which is the failure mode this whole thing exists to avoid."""
    _home, script = fake_env
    _path_is(monkeypatch, script.parent)  # only the venv bin, which is not under HOME
    linked, message = login_config.link_cli()
    assert not linked
    assert "PATH" in message and str(script.parent) in message


def test_a_link_that_does_not_resolve_afterwards_is_a_failure(fake_env, monkeypatch):
    """Ubuntu adds ~/.local/bin only if it existed at login, so a freshly created one is
    invisible to shells already running. Reporting success there hands someone a command
    that is still missing."""
    home, _ = fake_env
    _path_is(monkeypatch, home / ".local" / "bin")
    _resolves(monkeypatch, False)

    linked, message = login_config.link_cli()
    assert not linked
    assert "export PATH=" in message


def test_an_existing_correct_link_is_left_alone(fake_env, monkeypatch):
    home, script = fake_env
    link = home / ".local" / "bin" / "vast"
    link.symlink_to(script)
    _path_is(monkeypatch, home / ".local" / "bin")

    linked, message = login_config.link_cli()
    assert linked and "already resolves" in message


def test_a_stale_link_is_replaced(fake_env, monkeypatch):
    """A rebuilt venv leaves a dangling link; the next login should repair it."""
    home, script = fake_env
    link = home / ".local" / "bin" / "vast"
    link.symlink_to(home / "gone" / "vast")
    _path_is(monkeypatch, home / ".local" / "bin")
    _resolves(monkeypatch, True)

    linked, _ = login_config.link_cli()
    assert linked and link.resolve() == script


def test_a_missing_console_script_is_reported_not_linked(fake_env, monkeypatch):
    home, script = fake_env
    monkeypatch.setattr(login_config, "cli_path", lambda: script.parent / "absent")
    _path_is(monkeypatch, home / ".local" / "bin")

    linked, message = login_config.link_cli()
    assert not linked and "could not find" in message


def test_the_link_target_is_the_real_script_so_no_activation_is_needed(fake_env, monkeypatch):
    """The point of a symlink over a wrapper: the shebang keeps pointing at the venv's
    interpreter, so the command works with an empty environment."""
    home, script = fake_env
    _path_is(monkeypatch, home / ".local" / "bin")
    _resolves(monkeypatch, True)
    login_config.link_cli()

    link = home / ".local" / "bin" / "vast"
    assert os.readlink(link) == str(script)
