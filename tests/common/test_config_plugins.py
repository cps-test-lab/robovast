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


class _FakePopen:
    """Minimal stand-in for ``subprocess.Popen`` used by ``_install_target``.

    ``_install_target`` streams the child's merged stdout line by line and then
    calls ``wait()``. The fake exposes an iterable ``stdout`` and a ``wait`` that
    returns the configured return code, and records the ``cmd``/``env`` it was
    given via ``on_call`` for assertions.
    """

    def __init__(self, cmd, *, returncode=0, lines=(), on_call=None, **kw):
        if on_call is not None:
            on_call(cmd, kw)
        self.stdout = iter([f"{line}\n" for line in lines])
        self._returncode = returncode

    def wait(self):
        return self._returncode


def _fake_popen_factory(*, returncode=0, lines=(), on_call=None):
    def _factory(cmd, **kw):
        return _FakePopen(cmd, returncode=returncode, lines=lines,
                          on_call=on_call, **kw)
    return _factory


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
    # Prepended so the plugin's pinned deps win (safe: imports run only in the
    # isolated compose subprocess, never in the long-lived service).
    assert sys.path[0] == target
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
    assert sys.path[0] == target  # still put on sys.path (prepended)


def test_changed_specs_reinstall(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(cp, "_install_target",
                        lambda d, s: (os.makedirs(d, exist_ok=True), seen.append(list(s))))
    ensure_workspace_plugins(str(tmp_path), ["a==1"])
    ensure_workspace_plugins(str(tmp_path), ["a==2"])  # different hash -> reinstall
    assert seen == [["a==1"], ["a==2"]]


def test_install_failure_is_actionable(tmp_path, monkeypatch):
    import subprocess

    # pip exits non-zero and its (streamed) output carries a git auth failure.
    monkeypatch.setattr(subprocess, "Popen", _fake_popen_factory(
        returncode=1,
        lines=["fatal: could not read Username for 'https://github.com'"]))
    with pytest.raises(RuntimeError) as ei:
        ensure_workspace_plugins(str(tmp_path), ["x @ git+https://github.com/o/r@main"])
    msg = str(ei.value)
    assert "private repository" in msg      # auth detected
    assert "vast exec cluster setup" in msg  # remedy surfaced


def test_never_touches_active_venv(tmp_path, monkeypatch):
    """The install command targets the workspace dir, not global site-packages."""
    import subprocess
    captured = {}

    monkeypatch.setattr(subprocess, "Popen", _fake_popen_factory(
        on_call=lambda cmd, kw: captured.update(cmd=cmd)))
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

    def _cap(cmd, kw):
        captured["cmd"] = cmd
        captured["env"] = kw.get("env")

    monkeypatch.setattr(subprocess, "Popen", _fake_popen_factory(on_call=_cap))
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

    monkeypatch.setattr(subprocess, "Popen", _fake_popen_factory(
        on_call=lambda cmd, kw: captured.update(env=kw.get("env"))))
    ensure_workspace_plugins(str(tmp_path / "ws"), ["x @ git+https://github.com/o/r@main"])
    assert captured["env"]["ROBOVAST__GIT_TOKEN"] == "ghp_fromfile"


def test_no_token_no_askpass(tmp_path, monkeypatch):
    import subprocess
    monkeypatch.setattr(cp, "GIT_TOKEN_FILE", str(tmp_path / "absent"))
    for _var in cp.GIT_TOKEN_ENVS:  # no token from ANY accepted name
        monkeypatch.delenv(_var, raising=False)
    captured = {}

    monkeypatch.setattr(subprocess, "Popen", _fake_popen_factory(
        on_call=lambda cmd, kw: captured.update(env=kw.get("env"))))
    ensure_workspace_plugins(str(tmp_path / "ws"), ["some-pkg==1.0"])
    assert "GIT_ASKPASS" not in captured["env"]


def test_reads_token_from_conventional_env_name(tmp_path, monkeypatch):
    """Local compose honours the conventional GITHUB_TOKEN/GH_TOKEN, not only the
    canonical ROBOVAST_GIT_TOKEN — the same names the cluster setup accepts."""
    monkeypatch.setattr(cp, "GIT_TOKEN_FILE", str(tmp_path / "absent"))
    for var in cp.GIT_TOKEN_ENVS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GH_TOKEN", "ghp_conventional")
    assert cp._read_git_token() == "ghp_conventional"


def test_token_env_names_shared_with_cluster_setup():
    """Cluster and local read the token from the SAME set of env names (one source
    of truth), so a token that works for one works for the other."""
    from robovast.execution.cluster_execution import service_deploy as sd
    assert tuple(sd._GIT_TOKEN_HOST_ENVS) == tuple(cp.GIT_TOKEN_ENVS)


# -- postprocessing sees plugins: (install-if-absent + sys.path) --------------

def test_plugin_specs_from_vast(tmp_path):
    v = tmp_path / "p.vast"
    v.write_text("version: 1\nplugins:\n  - foo==1.2.3\n  - bar @ git+https://h/r@ref\n"
                 "  - ''\nexecution:\n  image: i\n")
    assert cp._plugin_specs_from_vast(str(v)) == ["foo==1.2.3", "bar @ git+https://h/r@ref"]
    # No plugins key / unreadable → empty (never raises).
    (tmp_path / "none.vast").write_text("version: 2\nexecution:\n  image: i\n")
    assert cp._plugin_specs_from_vast(str(tmp_path / "none.vast")) == []
    assert cp._plugin_specs_from_vast(str(tmp_path / "missing.vast")) == []


def test_ensure_postprocessing_plugins_installs_recorded_specs(tmp_path, monkeypatch):
    """A re-run reads plugins: from the campaign's .vast and installs-if-absent."""
    seen = {}
    monkeypatch.setattr(cp, "_install_target",
                        lambda d, s: (os.makedirs(d, exist_ok=True), seen.update(specs=list(s))))
    (tmp_path / "c.vast").write_text(
        "version: 1\nplugins:\n  - made-up-pp==9\nexecution:\n  image: i\n")
    cp.ensure_postprocessing_plugins(str(tmp_path))  # vast auto-discovered
    assert seen["specs"] == ["made-up-pp==9"]
    assert sys.path[0] == str(tmp_path / PLUGIN_DIRNAME)  # led sys.path


def test_ensure_postprocessing_plugins_prepends_existing_dir_without_specs(tmp_path):
    """No plugins: but a staged .robovast_plugins/ (compose) is still put on sys.path."""
    pd = tmp_path / PLUGIN_DIRNAME
    pd.mkdir()
    (tmp_path / "c.vast").write_text("version: 2\nexecution:\n  image: i\n")
    cp.ensure_postprocessing_plugins(str(tmp_path))
    assert sys.path[0] == str(pd)


def test_ensure_postprocessing_plugins_never_raises(tmp_path, monkeypatch):
    """A pip failure during postprocessing prep is swallowed (surfaces at plugin use)."""
    monkeypatch.setattr(cp, "ensure_workspace_plugins",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    (tmp_path / "c.vast").write_text(
        "version: 1\nplugins:\n  - x==1\nexecution:\n  image: i\n")
    cp.ensure_postprocessing_plugins(str(tmp_path))  # must not raise


# -- workspace-relative wheel paths ------------------------------------------
#
# `plugins:` documents "a workspace-relative path to a wheel you uploaded
# ('./plugins/my_plugin-1.0-py3-none-any.whl')", and relative is the whole point: the
# author cannot know where the service unpacked the workspace. pip resolves a relative
# path against the PROCESS cwd, which for the in-cluster service is /opt/robovast -- so
# the documented form failed with a FileNotFoundError naming a path nobody wrote.


def test_a_workspace_relative_wheel_resolves_against_the_vast_dir():
    from robovast.common.config_plugins import _resolve_local_specs
    got = _resolve_local_specs("/srv/sources/ws-1",
                               ["./plugins/my_plugin-1.0-py3-none-any.whl"])
    assert got == ["/srv/sources/ws-1/plugins/my_plugin-1.0-py3-none-any.whl"]


def test_index_pins_and_git_urls_are_left_alone():
    """A bare name must keep meaning the package, never a directory sharing its name."""
    from robovast.common.config_plugins import _resolve_local_specs
    specs = [
        "my_plugin==1.2.3",
        "scenario_mt @ git+https://github.com/org/repo@ref",
        "/absolute/already.whl",
    ]
    assert _resolve_local_specs("/srv/sources/ws-1", specs) == specs


def test_a_pep508_direct_reference_to_a_local_path_is_resolved():
    from robovast.common.config_plugins import _resolve_local_specs
    got = _resolve_local_specs("/srv/sources/ws-1", ["my_plugin @ ./plugins/p.whl"])
    assert got == ["my_plugin @ /srv/sources/ws-1/plugins/p.whl"]


def test_a_parent_relative_path_is_normalized():
    from robovast.common.config_plugins import _resolve_local_specs
    got = _resolve_local_specs("/srv/sources/ws-1/sub", ["../plugins/p.whl"])
    assert got == ["/srv/sources/ws-1/plugins/p.whl"]
