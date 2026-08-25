# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""``.vast`` ``plugins:`` install into a per-workspace venv, not the active one.

``ensure_workspace_plugins`` installs the declared specs into a venv under
``<vast_dir>/.robovast_plugins/`` (idempotent via a marker hashing the specs *and*
the environment) and puts that venv's ``site-packages`` on ``sys.path``. It never
touches the active venv, and a matching marker skips the install entirely. A failed
install raises an actionable error.

The venv is what makes the install resolve **against** the host rather than in
isolation from it, so a plugin that depends on robovast cannot be given a second copy
of it -- the shadowing that emptied ``robovast.search_strategies`` for a whole service
process. Several tests below exist only to hold that property.
"""

import os
import sys

import pytest

from robovast.common import config_plugins as cp
from robovast.common.config_plugins import (MARKER_NAME, PLUGIN_DIRNAME,
                                            ensure_workspace_plugins, plugin_site_dir)


@pytest.fixture(autouse=True)
def _restore_sys_path():
    before = list(sys.path)
    yield
    sys.path[:] = before


class _FakePopen:
    """Minimal stand-in for ``subprocess.Popen`` used by ``_install_into``.

    ``_install_into`` streams the child's merged stdout line by line and then
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


def test_installs_into_workspace_venv_and_adds_syspath(tmp_path, monkeypatch):
    calls = {}

    def fake_install(venv_dir, specs):
        calls["venv"] = venv_dir
        calls["specs"] = list(specs)

    monkeypatch.setattr(cp, "_install_into", fake_install)
    # no real dist named this -> _warn_if_already_loaded stays quiet
    result = ensure_workspace_plugins(str(tmp_path), ["totally-made-up-pkg==1.0"])

    site = plugin_site_dir(str(tmp_path))
    assert result == site
    assert calls["venv"] == str(tmp_path / PLUGIN_DIRNAME / cp.VENV_DIRNAME)
    assert calls["specs"] == ["totally-made-up-pkg==1.0"]
    # Prepended so the plugin's pinned deps win (safe: imports run only in the
    # isolated compose subprocess, never in the long-lived service).
    assert sys.path[0] == site
    assert os.path.isfile(str(tmp_path / PLUGIN_DIRNAME / MARKER_NAME))  # marker written


def test_marker_hit_skips_install(tmp_path, monkeypatch):
    # First call installs (stubbed) and writes the marker.
    monkeypatch.setattr(cp, "_install_into", lambda d, s: None)
    specs = ["made-up==2.0"]
    ensure_workspace_plugins(str(tmp_path), specs)

    # Second call with the same specs must NOT install again.
    def boom(*_a, **_k):
        raise AssertionError("install must be skipped on a marker hit")

    monkeypatch.setattr(cp, "_install_into", boom)
    site = ensure_workspace_plugins(str(tmp_path), specs)
    assert site == plugin_site_dir(str(tmp_path))
    assert sys.path[0] == site  # still put on sys.path (prepended)


def test_changed_specs_reinstall(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(cp, "_install_into", lambda d, s: seen.append(list(s)))
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


# The output a real failed clone produces, verbatim in shape: git's reason first, then
# pip's epilogue. The reason is more than eight lines from the end, which is what a
# tail-only excerpt cut -- and the diagnosis is made from the whole output for that reason.
def _clone_failure_lines(reason):
    return [
        "Collecting scenario_mt @ git+https://github.com/o/r@main",
        "  Running command git clone --filter=blob:none https://github.com/o/r",
        *reason,
        "error: subprocess-exited-with-error",
        "",
        "\u00d7 git clone --filter=blob:none https://github.com/o/r did not run successfully.",
        "\u2502 exit code: 128",
        "\u2570-> See above for output.",
        "",
        "note: This error originates from a subprocess, and is likely not a problem with pip.",
    ]


@pytest.mark.parametrize("reason,expect", [
    # A token that authenticates but does not cover the repository. git reports the status
    # as "returned error: 403", never as the phrase "403 forbidden".
    (["remote: Write access to repository not granted.",
      "fatal: unable to access 'https://github.com/o/r/': "
      "The requested URL returned error: 403"],
     "private repository"),
    # The same class seen as a 404, which is what a private repository tells a requester
    # that may not see it. Ambiguous with a wrong URL, so both are named.
    (["remote: Repository not found.",
      "fatal: repository 'https://github.com/o/r/' not found"],
     "could not be found"),
])
def test_private_repo_clone_failure_is_diagnosed(tmp_path, monkeypatch, reason, expect):
    """A credential that does not cover the repository is an auth failure, not a bad spec.

    Reported as "check that each spec is reachable" before this -- advice for a typo, on a
    spec that was spelled correctly -- because the signatures only matched the no-credential
    case and pip's epilogue is what the excerpt showed.
    """
    import subprocess

    monkeypatch.setattr(subprocess, "Popen", _fake_popen_factory(
        returncode=1, lines=_clone_failure_lines(reason)))
    with pytest.raises(RuntimeError) as ei:
        ensure_workspace_plugins(str(tmp_path), ["x @ git+https://github.com/o/r@main"])
    msg = str(ei.value)
    assert expect in msg
    assert "vast exec cluster setup" in msg          # the remedy, not just the symptom
    assert "each spec is reachable" not in msg       # not the wrong-URL advice
    # The cause survives the excerpt even though pip's epilogue follows it.
    assert reason[0] in msg


def test_never_touches_active_venv(tmp_path, monkeypatch):
    """The install command targets the workspace venv, not the active interpreter.

    ``--python`` and not ``--target``: ``--target`` makes pip force
    ``--ignore-installed``, which is what re-materialized the host -- including robovast
    itself -- into the workspace.
    """
    import subprocess
    captured = {}

    monkeypatch.setattr(subprocess, "Popen", _fake_popen_factory(
        on_call=lambda cmd, kw: captured.update(cmd=cmd)))
    ensure_workspace_plugins(str(tmp_path), ["some-pkg==1.0"])
    assert "--target" not in captured["cmd"]
    ti = captured["cmd"].index("--python")
    venv_dir = tmp_path / PLUGIN_DIRNAME / cp.VENV_DIRNAME
    assert captured["cmd"][ti + 1] == str(venv_dir / "bin" / "python")
    assert captured["cmd"][ti + 1] != sys.executable


def test_detects_already_installed_and_skips(tmp_path, monkeypatch):
    """CLI path: a plugin already installed (manually) is used, not re-fetched."""
    def boom(*_a, **_k):
        raise AssertionError("must not install an already-installed plugin")

    monkeypatch.setattr(cp, "_install_into", boom)
    # 'pytest' is installed in the test env.
    result = ensure_workspace_plugins(str(tmp_path), ["pytest"])
    assert result is None                                   # no workspace venv created
    assert not (tmp_path / PLUGIN_DIRNAME).exists()


def test_force_materializes_even_if_installed(tmp_path, monkeypatch):
    """Staging path: force installs every spec into the dir for the bare pod."""
    seen = {}
    monkeypatch.setattr(cp, "_install_into", lambda d, s: seen.update(specs=list(s)))
    site = ensure_workspace_plugins(str(tmp_path), ["pytest"], force=True)
    assert site == plugin_site_dir(str(tmp_path))
    assert seen["specs"] == ["pytest"]     # installed despite being importable


def test_force_install_warns_when_already_loaded(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(cp, "_install_into", lambda d, s: None)
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
    (tmp_path / "none.vast").write_text("version: 3\nexecution:\n  image: i\n")
    assert cp._plugin_specs_from_vast(str(tmp_path / "none.vast")) == []
    assert cp._plugin_specs_from_vast(str(tmp_path / "missing.vast")) == []


def test_ensure_plugins_importable_installs_recorded_specs(tmp_path, monkeypatch):
    """A re-run reads plugins: from the campaign's .vast and installs-if-absent."""
    seen = {}
    monkeypatch.setattr(cp, "_install_into", lambda d, s: seen.update(specs=list(s)))
    (tmp_path / "c.vast").write_text(
        "version: 1\nplugins:\n  - made-up-pp==9\nexecution:\n  image: i\n")
    cp.ensure_plugins_importable(str(tmp_path))  # vast auto-discovered
    assert seen["specs"] == ["made-up-pp==9"]
    # APPENDED, not prepended: every caller of ensure_plugins_importable runs in the
    # long-lived service, where sys.path is shared with concurrent campaigns.
    assert sys.path[-1] == plugin_site_dir(str(tmp_path))
    assert sys.path[0] != plugin_site_dir(str(tmp_path))


def test_a_leftover_dir_registering_nothing_is_not_put_on_sys_path(tmp_path):
    """A .vast that declares no plugins must not reorder imports for the process.

    An empty (or plugin-less) leftover directory used to be prepended anyway, which is
    how one workspace's install reached a campaign that never asked for one.
    """
    site = plugin_site_dir(str(tmp_path))
    os.makedirs(site)
    (tmp_path / "c.vast").write_text("version: 3\nexecution:\n  image: i\n")
    cp.ensure_plugins_importable(str(tmp_path))
    assert site not in sys.path


def test_a_leftover_dir_registering_plugins_is_still_used(tmp_path):
    """The converse: a real staged install is found without re-declaring it."""
    site = plugin_site_dir(str(tmp_path))
    di = os.path.join(site, "made_up-1.0.dist-info")
    os.makedirs(di)
    with open(os.path.join(di, "METADATA"), "w", encoding="utf-8") as f:
        f.write("Metadata-Version: 2.1\nName: made_up\nVersion: 1.0\n")
    with open(os.path.join(di, "entry_points.txt"), "w", encoding="utf-8") as f:
        f.write("[robovast.variation_types]\nMadeUp = made_up:MadeUp\n")
    (tmp_path / "c.vast").write_text("version: 3\nexecution:\n  image: i\n")
    cp.ensure_plugins_importable(str(tmp_path))
    assert sys.path[-1] == site
    assert cp.staged_variation_type_names(str(tmp_path)) == {"MadeUp"}


def test_ensure_plugins_importable_never_raises(tmp_path, monkeypatch):
    """A pip failure during postprocessing prep is swallowed (surfaces at plugin use)."""
    monkeypatch.setattr(cp, "ensure_workspace_plugins",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    (tmp_path / "c.vast").write_text(
        "version: 1\nplugins:\n  - x==1\nexecution:\n  image: i\n")
    cp.ensure_plugins_importable(str(tmp_path))  # must not raise


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


# ---------------------------------------------------------------------------
# The shadowing regression: a plugin that depends on robovast must not be given
# a second copy of it. These hold the property the venv exists for.
# ---------------------------------------------------------------------------

def test_the_workspace_venv_sees_the_host_interpreters_packages(tmp_path):
    """The venv must resolve against the *running* interpreter, not the base one.

    ``--system-site-packages`` alone adds ``sys.base_prefix``'s site directories. When
    robovast itself runs from a venv -- every developer checkout -- that is the system
    Python, which does not have robovast; pip would then find nothing to satisfy a
    plugin's ``robovast>=1.0.0`` and install a second copy. The explicit ``.pth`` is
    what makes this correct in both deployments, so assert the outcome, not the flag.
    """
    import subprocess
    venv_dir = str(tmp_path / PLUGIN_DIRNAME / cp.VENV_DIRNAME)
    site = cp._ensure_venv(venv_dir)
    assert os.path.isfile(os.path.join(site, cp.HOST_PTH_NAME))

    seen = subprocess.run(
        [cp._venv_python(venv_dir), "-c",
         "import importlib.metadata as m; print(m.version('robovast'))"],
        capture_output=True, text=True, check=False)
    assert seen.returncode == 0, seen.stderr
    from importlib.metadata import version
    assert seen.stdout.strip() == version("robovast")


def test_a_plugin_depending_on_robovast_resolves_to_the_host(tmp_path):
    """pip must treat the host's robovast as satisfying ``robovast>=1.0.0``.

    Driven with ``--no-index`` so the assertion cannot be met by a download: if the
    workspace venv could not see the host, pip would have to fetch a robovast and would
    fail instead. Under ``pip install --target`` it did exactly that -- ``--target``
    forces ``--ignore-installed`` -- and the fetched 1.0.0's entry points then replaced
    the host's for the whole process, emptying ``robovast.search_strategies``.
    """
    import subprocess
    venv_dir = str(tmp_path / PLUGIN_DIRNAME / cp.VENV_DIRNAME)
    site = cp._ensure_venv(venv_dir)

    done = subprocess.run(
        [sys.executable, "-m", "pip", "--python", cp._venv_python(venv_dir), "install",
         "--no-index", "--no-deps", "--disable-pip-version-check", "robovast>=1.0.0"],
        capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "already satisfied" in (done.stdout + done.stderr).lower()

    from importlib.metadata import distributions
    assert [d.metadata["Name"] for d in distributions(path=[site])] == []


def test_a_pre_venv_target_tree_is_reclaimed(tmp_path):
    """A flat ``pip --target`` tree from an older robovast is removed, marker and all.

    It is already inert -- the importable directory moved into the venv -- but a real
    one runs to about a gigabyte, and leaving it would make ``.robovast_plugins/`` mean
    two things at once. The marker goes too: it describes an install this layout no
    longer has, and keeping it could skip the venv install.
    """
    pd = tmp_path / PLUGIN_DIRNAME
    (pd / "robovast-1.0.0.dist-info").mkdir(parents=True)
    (pd / "robovast-1.0.0.dist-info" / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: robovast\nVersion: 1.0.0\n")
    (pd / "robovast").mkdir()
    (pd / MARKER_NAME).write_text("stale")

    cp._reclaim_pre_venv_layout(str(tmp_path))

    assert not (pd / "robovast").exists()
    assert not (pd / "robovast-1.0.0.dist-info").exists()
    assert not (pd / MARKER_NAME).exists()


def test_reclaiming_leaves_a_venv_alone(tmp_path):
    """Only the flat layout is reclaimed; a current workspace must survive it."""
    venv_dir = tmp_path / PLUGIN_DIRNAME / cp.VENV_DIRNAME
    venv_dir.mkdir(parents=True)
    (venv_dir / "pyvenv.cfg").write_text("home = /usr\n")
    (tmp_path / PLUGIN_DIRNAME / "leftover-1.0.dist-info").mkdir()

    cp._reclaim_pre_venv_layout(str(tmp_path))

    assert (venv_dir / "pyvenv.cfg").exists()


def test_the_marker_covers_the_environment_not_only_the_specs(monkeypatch):
    """Same specs against a different interpreter are a different install.

    The install is resolved *against the host*, so the host is part of what the marker
    describes. Hashing the specs alone let a tree built by a 3.12 service be reused
    verbatim by a 3.13 one -- marker matching, no reinstall, and a ``site-packages``
    that interpreter cannot import.
    """
    specs = ["some-pkg==1.0"]
    here = cp._spec_hash(specs)
    assert cp._spec_hash(specs) == here            # stable
    assert cp._spec_hash(list(reversed(specs + ["b==2"]))) == cp._spec_hash(specs + ["b==2"])

    real = cp.sysconfig.get_paths
    monkeypatch.setattr(cp.sysconfig, "get_paths",
                        lambda *a, **k: {**real(*a, **k), "purelib": "/somewhere/else"})
    assert cp._spec_hash(specs) != here


def test_a_plugin_declaring_a_dependency_on_robovast_is_reported(tmp_path):
    """The declaration is harmless now, but only its author can remove it.

    Read off installed metadata rather than pip's output, so a ``git+https`` spec that
    pip logged under a different display name is still attributed correctly.
    """
    site = plugin_site_dir(str(tmp_path))
    di = os.path.join(site, "needy-1.0.dist-info")
    os.makedirs(di)
    with open(os.path.join(di, "METADATA"), "w", encoding="utf-8") as f:
        f.write("Metadata-Version: 2.1\nName: needy\nVersion: 1.0\n"
                "Requires-Dist: shapely (>=2.0)\nRequires-Dist: robovast (>=1.0.0)\n")

    assert cp.host_dependent_plugins(str(tmp_path)) == {"needy": "robovast (>=1.0.0)"}


def test_a_plugin_registering_robovast_entry_points_is_not_mistaken_for_the_host(tmp_path):
    """The false positive to avoid: a real plugin registers ``robovast.*`` groups.

    ``scenario_mt`` declares ``robovast.variation_types`` and is exactly what
    ``plugins:`` is for. Nothing here may treat that as a host copy.
    """
    site = plugin_site_dir(str(tmp_path))
    di = os.path.join(site, "scenario_mt-1.0.dist-info")
    os.makedirs(di)
    with open(os.path.join(di, "METADATA"), "w", encoding="utf-8") as f:
        f.write("Metadata-Version: 2.1\nName: scenario_mt\nVersion: 1.0\n")
    with open(os.path.join(di, "entry_points.txt"), "w", encoding="utf-8") as f:
        f.write("[robovast.variation_types]\nSemanticGeneration = scenario_mt:SG\n")

    assert cp.host_dependent_plugins(str(tmp_path)) == {}
    assert cp.staged_variation_type_names(str(tmp_path)) == {"SemanticGeneration"}
    assert cp._registers_plugins(site) is True
