# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for ``show_gui``: putting the simulator's window on the serve host's display.

The failure this whole feature has to avoid is a *silent* one — a run that was asked to
show a window, started cleanly, and drew nothing. Every check here is one of the four ways
that could happen:

- a lane that has no screen accepting the request anyway (cluster, in-cluster service);
- a serve host with no display accepting it;
- the scenario staying headless because the project's overrides were not applied;
- a held exec container being reused for a windowed call when its X socket was never
  mounted — mounts exist only from container creation.
"""

import pytest

from robovast.common.execution import local_parameter_overrides
from robovast.common.host_display import host_display, require_host_display
from robovast.service.client import LocalTransport
from robovast.service.cluster_service import ClusterService
from robovast.service.container_exec import ExecSpec
from robovast.service.docker_exec_lane import DockerExecLane
from robovast.service.interface import CreateCampaignRequest, ExecRequest
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore


@pytest.fixture
def _store(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "robovast.common.cli.project_config.ProjectConfig.load",
        staticmethod(lambda *a, **k: None))
    return WorkspaceStore(registry=WorkspaceRegistry(root=tmp_path / "workspaces"))


@pytest.fixture
def local(_store):
    return LocalTransport(store=_store)


@pytest.fixture
def cluster(_store):
    # reap_on_start would talk to a cluster; nothing here needs a live one.
    return ClusterService(store=_store, reap_on_start=False)


# -- the display gate --------------------------------------------------------


def test_a_missing_display_is_detected_from_env_and_from_the_socket(monkeypatch, tmp_path):
    monkeypatch.setenv("DISPLAY", ":3")
    assert host_display() == ":3"
    # DISPLAY unset is not the same as "no display": a daemon started outside a desktop
    # session still reaches a running X server, which the socket is the evidence of.
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr("robovast.common.host_display._X11_SOCKET_GLOB",
                        str(tmp_path / "X*"))
    assert host_display() == ""
    (tmp_path / "X0").write_text("")
    assert host_display() == ":0"


def test_the_refusal_says_the_window_would_open_on_the_serve_host(monkeypatch, tmp_path):
    # For a tunnelled service the caller is looking at a perfectly good display of their
    # own, so a bare "no display available" reads as a bug rather than as the answer.
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr("robovast.common.host_display._X11_SOCKET_GLOB",
                        str(tmp_path / "X*"))
    with pytest.raises(ValueError, match="serve host"):
        require_host_display(what="show_gui")


# -- which lane may honour it ------------------------------------------------


def test_the_local_lane_maps_show_gui_onto_the_runs_gui_option(local):
    # The request's outward name meets the run machinery's internal one here, and nowhere
    # else — run.sh's flag is --no-gui and could not be renamed with it.
    assert local._run_options(
        CreateCampaignRequest(workspace_id="w", show_gui=True)).gui is True
    assert local._run_options(CreateCampaignRequest(workspace_id="w")).gui is False


@pytest.mark.parametrize("request_kind", ["campaign", "exec"])
def test_a_cluster_service_refuses_rather_than_running_windowless(cluster, request_kind):
    req = (CreateCampaignRequest(workspace_id="w", show_gui=True)
           if request_kind == "campaign"
           else ExecRequest(command="true", workspace_id="w", show_gui=True))
    with pytest.raises(ValueError, match="local `vast serve`"):
        cluster._admit_show_gui(req)


def test_a_cluster_service_still_accepts_a_plain_request(cluster):
    cluster._admit_show_gui(CreateCampaignRequest(workspace_id="w"))


def test_a_local_service_without_a_display_refuses_too(local, monkeypatch, tmp_path):
    # An explicit show_gui is the caller's request, so it is refused rather than quietly
    # downgraded — unlike `vast execution local run`, where GUI is a *default* and a
    # headless build machine must keep working (see test_cli_show_gui.py).
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr("robovast.common.host_display._X11_SOCKET_GLOB",
                        str(tmp_path / "X*"))
    with pytest.raises(ValueError, match="serve host"):
        local._admit_show_gui(CreateCampaignRequest(workspace_id="w", show_gui=True))


# -- the config block -------------------------------------------------------


def _campaign(local):
    return {"execution": {"local": local}}


def test_the_gui_overrides_apply_only_with_a_display():
    data = _campaign({"parameter_overrides": [{"use_rviz": "True"}],
                      "gui": {"parameter_overrides": [{"headless": "False"}]}})
    # The existing key keeps its documented meaning — every local run — so adding a window
    # to a project cannot change what its headless runs do.
    assert local_parameter_overrides(data, gui=False) == [{"use_rviz": "True"}]
    assert local_parameter_overrides(data, gui=True) == [{"use_rviz": "True"},
                                                        {"headless": "False"}]


def test_the_gui_block_wins_where_both_set_a_parameter():
    data = _campaign({"parameter_overrides": [{"headless": "True"}],
                      "gui": {"parameter_overrides": [{"headless": "False"}]}})
    # _apply_local_parameter_overrides merges in order, so last wins.
    assert local_parameter_overrides(data, gui=True)[-1] == {"headless": "False"}


def test_a_project_with_no_local_block_is_untouched_either_way():
    assert local_parameter_overrides({"execution": {}}, gui=True) == []
    assert local_parameter_overrides({"execution": {}}, gui=False) == []


def test_the_show_gui_note_fires_only_when_the_project_declares_no_gui_block():
    from robovast.service.local_transport import _show_gui_note
    windowed = CreateCampaignRequest(workspace_id="w", show_gui=True)
    assert "execution.local.gui" in _show_gui_note(windowed, _campaign({}))
    assert _show_gui_note(
        windowed, _campaign({"gui": {"parameter_overrides": [{"headless": "False"}]}})) == ""
    # No note when nothing was asked for: it would be noise on every launch.
    assert _show_gui_note(CreateCampaignRequest(workspace_id="w"), _campaign({})) == ""


# -- the exec lane's mounts -------------------------------------------------


def _spec(**kw):
    return ExecSpec(image="img", command="true", config_dir="/tmp/cfg", env={}, **kw)


def test_the_x_socket_is_mounted_only_for_a_windowed_exec(monkeypatch):
    monkeypatch.setattr("robovast.service.docker_exec_lane.grant_local_access",
                        lambda: None)
    windowed = " ".join(DockerExecLane()._common_run_args(_spec(gui=True)))
    assert "/tmp/.X11-unix:/tmp/.X11-unix:rw" in windowed
    plain = " ".join(DockerExecLane()._common_run_args(_spec()))
    assert ".X11-unix" not in plain


def test_a_windowed_exec_grants_x_access_itself(monkeypatch):
    # There is no run.sh on this path to do it, and without the grant the X server refuses
    # the container — which looks exactly like a run that simply drew nothing.
    granted = []
    monkeypatch.setattr("robovast.service.docker_exec_lane.grant_local_access",
                        lambda: granted.append(True))
    DockerExecLane()._common_run_args(_spec(gui=True))
    assert granted == [True]
    DockerExecLane()._common_run_args(_spec())
    assert granted == [True]


def test_changing_show_gui_is_a_different_container(local, monkeypatch):
    # The identity tuple must carry it: exec_in_held can add env but not mounts, so reusing
    # a mount-less container for a windowed call would silently show nothing.
    calls = []

    class _Mgr:
        def run(self, spec, limit_s, *, keep_alive, identity):
            calls.append(identity)
            return (0, "", "", False)

        def state(self):
            return None

    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(LocalTransport, "_exec_manager", property(lambda self: _Mgr()))
    monkeypatch.setattr(LocalTransport, "_exec_vast_file",
                        lambda self, request: "x.vast")
    monkeypatch.setattr(LocalTransport, "_exec_image", lambda self, vast, container=None: "img")
    monkeypatch.setattr("robovast.service.container_exec.validate", lambda request: None)
    monkeypatch.setattr(
        "robovast.service.container_exec.stage",
        lambda *a, **kw: (_spec(gui=kw.get("gui", False)), {}, 300, "command"))
    monkeypatch.setattr("robovast.service.container_exec.result_from",
                        lambda out, **kw: out)

    for show_gui in (False, True):
        local.exec_in_container(ExecRequest(command="true", workspace_id="",
                                            show_gui=show_gui))
    assert calls[0] != calls[1]
