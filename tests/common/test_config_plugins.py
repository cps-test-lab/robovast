# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""``.vast`` ``plugins:`` install into a per-workspace dir, not the active venv.

``ensure_workspace_plugins`` installs the declared specs into
``<vast_dir>/.robovast_plugins/`` with ``pip install --target`` (idempotent via a
spec-hash marker) and puts that dir on ``sys.path``. It never touches the active
venv, and a matching marker skips the install entirely (the offline controller-pod
path). A failed install raises an actionable error.
"""

import os
import sys

import pytest

from robovast.common import config_plugins as cp
from robovast.common.config_plugins import (MARKER_NAME, PLUGIN_DIRNAME,
                                            ensure_workspace_plugins)


@pytest.fixture(autouse=True)
def _restore_sys_path():
    before = list(sys.path)
    yield
    sys.path[:] = before


def test_no_plugins_is_noop(tmp_path):
    assert ensure_workspace_plugins(str(tmp_path), None) is None
    assert ensure_workspace_plugins(str(tmp_path), []) is None
    assert not (tmp_path / PLUGIN_DIRNAME).exists()


def test_installs_into_workspace_dir_and_adds_syspath(tmp_path, monkeypatch):
    calls = {}

    def fake_install(target_dir, specs):
        os.makedirs(target_dir, exist_ok=True)
        calls["target"] = target_dir
        calls["specs"] = list(specs)

    monkeypatch.setattr(cp, "_install_target", fake_install)
    # no real dist named this -> _warn_if_already_loaded stays quiet
    result = ensure_workspace_plugins(str(tmp_path), ["totally-made-up-pkg==1.0"])

    target = str(tmp_path / PLUGIN_DIRNAME)
    assert result == target
    assert calls["target"] == target and calls["specs"] == ["totally-made-up-pkg==1.0"]
    assert sys.path[0] == target                      # prepended
    assert os.path.isfile(os.path.join(target, MARKER_NAME))  # marker written


def test_marker_hit_skips_install(tmp_path, monkeypatch):
    # First call installs (stubbed) and writes the marker.
    monkeypatch.setattr(cp, "_install_target",
                        lambda d, s: os.makedirs(d, exist_ok=True))
    specs = ["made-up==2.0"]
    ensure_workspace_plugins(str(tmp_path), specs)

    # Second call with the same specs must NOT install again (offline pod path).
    def boom(*_a, **_k):
        raise AssertionError("install must be skipped on a marker hit")

    monkeypatch.setattr(cp, "_install_target", boom)
    target = ensure_workspace_plugins(str(tmp_path), specs)
    assert target == str(tmp_path / PLUGIN_DIRNAME)
    assert sys.path[0] == target  # still put on sys.path


def test_changed_specs_reinstall(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(cp, "_install_target",
                        lambda d, s: (os.makedirs(d, exist_ok=True), seen.append(list(s))))
    ensure_workspace_plugins(str(tmp_path), ["a==1"])
    ensure_workspace_plugins(str(tmp_path), ["a==2"])  # different hash -> reinstall
    assert seen == [["a==1"], ["a==2"]]


def test_install_failure_is_actionable(tmp_path, monkeypatch):
    import subprocess

    def failing_run(cmd, **kw):
        raise subprocess.CalledProcessError(
            1, cmd, stderr="fatal: could not read Username for 'https://github.com'")

    monkeypatch.setattr(subprocess, "run", failing_run)
    with pytest.raises(RuntimeError) as ei:
        ensure_workspace_plugins(str(tmp_path), ["x @ git+https://github.com/o/r@main"])
    msg = str(ei.value)
    assert "private repository" in msg      # auth detected
    assert "vast exec cluster setup" in msg  # remedy surfaced


def test_never_touches_active_venv(tmp_path, monkeypatch):
    """The install command targets the workspace dir, not global site-packages."""
    import subprocess
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        class R:  # minimal CompletedProcess stand-in
            returncode = 0
        return R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    ensure_workspace_plugins(str(tmp_path), ["some-pkg==1.0"])
    assert "--target" in captured["cmd"]
    ti = captured["cmd"].index("--target")
    assert captured["cmd"][ti + 1] == str(tmp_path / PLUGIN_DIRNAME)


def test_detects_already_installed_and_skips(tmp_path, monkeypatch):
    """CLI path: a plugin already installed (manually) is used, not re-fetched."""
    def boom(*_a, **_k):
        raise AssertionError("must not install an already-installed plugin")

    monkeypatch.setattr(cp, "_install_target", boom)
    # 'pytest' is installed in the test env.
    result = ensure_workspace_plugins(str(tmp_path), ["pytest"])
    assert result is None                                   # no workspace dir created
    assert not (tmp_path / PLUGIN_DIRNAME).exists()


def test_force_materializes_even_if_installed(tmp_path, monkeypatch):
    """Staging path: force installs every spec into the dir for the bare pod."""
    seen = {}
    monkeypatch.setattr(cp, "_install_target",
                        lambda d, s: (os.makedirs(d, exist_ok=True), seen.update(specs=list(s))))
    target = ensure_workspace_plugins(str(tmp_path), ["pytest"], force=True)
    assert target == str(tmp_path / PLUGIN_DIRNAME)
    assert seen["specs"] == ["pytest"]     # installed despite being importable


def test_force_install_warns_when_already_loaded(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(cp, "_install_target",
                        lambda d, s: os.makedirs(d, exist_ok=True))
    with caplog.at_level("WARNING"):
        ensure_workspace_plugins(str(tmp_path), ["pytest"], force=True)
    assert any("already present" in r.message for r in caplog.records)


def test_git_token_never_in_process_env_only_scoped_to_subprocess(tmp_path, monkeypatch):
    """The token reaches only the pip subprocess (via GIT_ASKPASS), never gitconfig
    or the parent env / command line."""
    import subprocess

    # Fallback env token present in the parent — must NOT be inherited by the child.
    monkeypatch.setenv(cp.GIT_TOKEN_ENV, "ghp_secret")
    monkeypatch.setattr(cp, "GIT_TOKEN_FILE", str(tmp_path / "nope"))  # no file → env fallback
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        captured["env"] = kw.get("env")
        class R:
            returncode = 0
        return R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    ensure_workspace_plugins(str(tmp_path), ["x @ git+https://github.com/o/r@main"])

    child_env = captured["env"]
    # Token is not passed through the normal env var (would be inherited widely)…
    assert cp.GIT_TOKEN_ENV not in child_env
    # …it is provided only to the askpass helper, scoped to this subprocess.
    assert child_env["ROBOVAST__GIT_TOKEN"] == "ghp_secret"
    assert child_env["GIT_ASKPASS"].endswith("askpass.sh")
    assert child_env["GIT_TERMINAL_PROMPT"] == "0"
    # Never on the command line (visible via ps).
    assert not any("ghp_secret" in part for part in captured["cmd"])


def test_git_token_read_from_mounted_file(tmp_path, monkeypatch):
    import subprocess
    token_file = tmp_path / "token"
    token_file.write_text("ghp_fromfile\n")
    monkeypatch.setattr(cp, "GIT_TOKEN_FILE", str(token_file))
    monkeypatch.delenv(cp.GIT_TOKEN_ENV, raising=False)
    captured = {}

    def fake_run(cmd, **kw):
        captured["env"] = kw.get("env")
        class R:
            returncode = 0
        return R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    ensure_workspace_plugins(str(tmp_path / "ws"), ["x @ git+https://github.com/o/r@main"])
    assert captured["env"]["ROBOVAST__GIT_TOKEN"] == "ghp_fromfile"


def test_no_token_no_askpass(tmp_path, monkeypatch):
    import subprocess
    monkeypatch.setattr(cp, "GIT_TOKEN_FILE", str(tmp_path / "absent"))
    monkeypatch.delenv(cp.GIT_TOKEN_ENV, raising=False)
    captured = {}

    def fake_run(cmd, **kw):
        captured["env"] = kw.get("env")
        class R:
            returncode = 0
        return R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    ensure_workspace_plugins(str(tmp_path / "ws"), ["some-pkg==1.0"])
    assert "GIT_ASKPASS" not in captured["env"]
