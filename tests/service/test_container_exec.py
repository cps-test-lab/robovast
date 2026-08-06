# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for container exec: the diagnostic that runs one command in an experiment image.

Two things these guard that are easy to lose:

- it must stay **unable to produce a campaign** — no campaign dir, no ``/out``, and an
  ``OUTPUT_DIR`` that dies with the container;
- the container's *lifetime* rules, which are subtle enough that the naive version of
  each was wrong first: a one-shot must not inherit a held container, an idle reap must
  not kill a running scenario, and replacing a container that is running something must
  be refused rather than inferred from a changed argument.

The lane is faked here (a real one needs Docker); ``tests/service`` stays hermetic.
"""

import os
import time

import pytest

from robovast.service import container_exec as ce
from robovast.service.interface import ExecRequest

VAST = "configs/examples/ros2_basic/ros2_service.vast"


class FakeLane:
    """Records what a lane was asked to do, and can pretend to be busy."""

    def __init__(self):
        self.alive = False
        self.busy = False
        self.starts = []
        self.execs = []
        self.once = []
        self.stops = 0

    def run_once(self, spec, limit_s):
        self.once.append((spec.command, limit_s))
        return (0, "once-out", "", False)

    def start_held(self, spec, deadline_s):
        self.alive = True
        self.starts.append((spec.config_name, deadline_s))

    def exec_in_held(self, spec, limit_s, detach):
        self.execs.append((spec.command, limit_s, detach))
        return (0, "held-out", "", False)

    def stop_held(self):
        self.stops += 1
        was, self.alive = self.alive, False
        return was

    def held_workload_running(self):
        return self.busy


def _spec(command="ls", config_name="", image="img:1"):
    spec = ce.ExecSpec(image=image, command=command, config_dir="", env={},
                       config_name=config_name)
    return spec


# -- request validation: refuse, never guess ---------------------------------


@pytest.mark.parametrize("kwargs, expected", [
    ({"command": "ls"}, "no source named"),
    ({"command": "ls", "workspace_id": "w", "campaign_id": "c"}, "one source, not both"),
    ({"command": "  ", "workspace_id": "w"}, "nothing to run"),
    ({"command": "ls", "campaign_id": "c", "config_path": "p.vast"}, "needs workspace_id"),
])
def test_a_request_that_cannot_be_honoured_is_refused(kwargs, expected):
    with pytest.raises(ValueError, match=expected):
        ce.validate(ExecRequest(**kwargs))


@pytest.mark.parametrize("kwargs", [
    {"command": "ls", "workspace_id": "w"},
    {"command": "", "workspace_id": "w", "config_name": "c1"},
    {"command": "", "campaign_id": "c", "config_name": "c1"},
])
def test_a_well_formed_request_is_accepted(kwargs):
    ce.validate(ExecRequest(**kwargs))


# -- limits: derived, and the source is reported -----------------------------


def test_a_command_gets_the_fixed_cap_whatever_the_project_says():
    # The project's timeout bounds its *scenario*; it says nothing about how long a
    # diagnostic command may take, and a command needing longer wants a campaign.
    assert ce.derive_limit({"execution": {"timeout": 900}}, "ls") == (
        ce.COMMAND_LIMIT_S, ce.LIMIT_SOURCE_COMMAND)


def test_a_scenario_uses_the_projects_own_timeout():
    assert ce.derive_limit({"execution": {"timeout": 900}}, "") == (
        900, ce.LIMIT_SOURCE_CONFIG)


def test_an_absent_timeout_is_reported_as_a_default_not_silently_borrowed():
    # Never the cluster campaign lane's 1-hour fallback: an hour of life for a
    # diagnostic container is a leak. Reporting "default" is what tells the caller the
    # remedy is to set execution.timeout.
    limit, source = ce.derive_limit({"execution": {}}, "")
    assert (limit, source) == (ce.DEFAULT_SCENARIO_LIMIT_S, ce.LIMIT_SOURCE_DEFAULT)
    assert limit != 3600


def test_a_projects_long_timeout_is_not_clamped_by_the_container_deadline():
    # Clamping here would ignore a value the caller did supply.
    assert ce.deadline_for(900) > 900
    assert ce.deadline_for(60) == ce.IDLE_WAIT_CAP_S


# -- the environment a command runs under ------------------------------------


def test_output_never_points_at_the_campaign_mount():
    env = ce.build_env({}, {}, staged_config=True)
    assert env["OUTPUT_DIR"] == ce.OUTPUT_DIR
    assert not env["OUTPUT_DIR"].startswith("/out")
    assert env["SCENARIO_OUTPUT_DIR"] == ce.OUTPUT_DIR


def test_sysinfo_is_disabled_only_when_nothing_is_staged():
    # The bare-image case mounts no /config/collect_sysinfo.py, and under `set -e` the
    # entrypoint would abort on it before running the requested command. With a config
    # staged the script is there, so the run behaves like a campaign's.
    assert ce.build_env({}, {}, staged_config=False)["COLLECT_SYSINFO"] == "false"
    assert "COLLECT_SYSINFO" not in ce.build_env({}, {}, staged_config=True)


def test_the_projects_own_env_and_pre_command_still_apply():
    env = ce.build_env({}, {"env": [{"MY_VAR": "1"}], "pre_command": "/setup.sh"},
                       staged_config=True)
    assert env["MY_VAR"] == "1"
    assert env["PRE_COMMAND"] == "/setup.sh"


def test_no_virtual_display_is_started():
    assert ce.build_env({}, {}, staged_config=True)["ENABLE_X11"] == "false"


def test_a_windowed_exec_uses_the_hosts_display_not_a_virtual_one(monkeypatch):
    # Xvfb stays off with gui too, and that is the point rather than an oversight: the
    # container draws on the host's X server through the socket the lane mounts, and a
    # virtual framebuffer would shadow exactly that.
    monkeypatch.setenv("DISPLAY", ":7")
    env = ce.build_env({}, {}, staged_config=True, gui=True)
    assert env["ENABLE_X11"] == "false"
    assert env["DISPLAY"] == ":7"


def test_without_gui_no_display_is_handed_to_the_container(monkeypatch):
    monkeypatch.setenv("DISPLAY", ":7")
    assert "DISPLAY" not in ce.build_env({}, {}, staged_config=True)


# -- argv: the run's entrypoint, not a hand-rolled prelude -------------------


def test_a_command_runs_through_the_entrypoint_via_bash():
    # Two things ride on this shape: the entrypoint sets up the run's environment (so
    # `ros2` is on PATH at all), and its stdout redirection is skipped precisely when
    # the argv mentions a shell — which is what returns a command's output to the caller.
    argv = _spec(command="ros2 pkg list").entrypoint_argv()
    assert argv == ["/config/entrypoint.sh", "bash", "-c", "ros2 pkg list"]
    assert "bash" in argv


def test_an_empty_command_runs_the_scenario_by_giving_the_entrypoint_no_argv():
    spec = _spec(command="")
    assert spec.entrypoint_argv() == ["/config/entrypoint.sh"]
    assert spec.runs_scenario


# -- the Docker lane's argv (no daemon needed to check its shape) -------------


def test_the_entrypoint_is_always_invoked_through_bash():
    """The staged ``entrypoint.sh`` is written 0644, so it must not be run directly.

    This was a real silent failure: the detached form started
    ``setsid nohup /config/entrypoint.sh``, which died on the missing exec bit *after*
    the exec had already returned 0 — so a scenario that never ran reported as started,
    and the only symptom was an ``OUTPUT_DIR`` with no results in it.
    """
    from robovast.service.docker_exec_lane import DockerExecLane
    lane = DockerExecLane()
    captured = []

    def fake_capture(cmd, limit_s):
        captured.append(cmd)
        return (0, "", "", False)

    import robovast.service.docker_exec_lane as mod
    original, mod._capture = mod._capture, fake_capture
    try:
        lane.exec_in_held(_spec(command="ls"), 300, detach=False)
        foreground = captured[-1]
        assert "/bin/bash" in foreground
        assert foreground.index("/bin/bash") < foreground.index("/config/entrypoint.sh")

        lane.exec_in_held(_spec(command=""), 300, detach=True)
        script = captured[-1][-1]
        assert "setsid nohup /bin/bash /config/entrypoint.sh" in script
    finally:
        mod._capture = original


def test_a_detached_scenario_that_dies_immediately_is_reported_as_a_failure():
    """Starting is not the same as running, and the difference must not be silent."""
    from robovast.service.docker_exec_lane import DockerExecLane
    import robovast.service.docker_exec_lane as mod
    captured = []

    def fake_capture(cmd, limit_s):
        captured.append(cmd)
        return (0, "", "", False)

    original, mod._capture = mod._capture, fake_capture
    try:
        DockerExecLane().exec_in_held(_spec(command=""), 300, detach=True)
        script = captured[-1][-1]
        # It checks the child is still alive, and surfaces the log when it is not.
        assert 'kill -0 "$pid"' in script
        assert "exited immediately" in script
        assert "tail -20" in script
        assert "exit 1" in script
    finally:
        mod._capture = original


def test_both_lanes_start_a_detached_scenario_the_same_way():
    """The duplication this removes is what let a fix reach only one lane.

    Each lane had its own copy of the background-start shell, so the liveness check that
    turned "silently never started" into a reported failure existed locally and was
    missing in-cluster.
    """
    import inspect

    from robovast.service.docker_exec_lane import DockerExecLane
    from robovast.service.kube_exec_lane import KubeExecLane
    for lane in (DockerExecLane, KubeExecLane):
        source = inspect.getsource(lane.exec_in_held)
        assert "detached_start_script()" in source, f"{lane.__name__} rolls its own"
        assert "foreground_argv()" in source
        # ``nohup``/``mkdir -p`` only appear where the shell is actually assembled, so
        # this catches a reintroduced copy without tripping over prose about it.
        for built_inline in ("nohup", "mkdir -p", "kill -0"):
            assert built_inline not in source, \
                f"{lane.__name__} builds the start script inline again ({built_inline})"


def test_the_config_mount_is_read_only_and_at_one_path():
    from robovast.service.docker_exec_lane import DockerExecLane
    spec = _spec()
    spec.config_dir = "/host/staging/config"
    spec.workspace_dir, spec.workspace_id = "/host/ws", "ws1"
    args = DockerExecLane()._common_run_args(spec)
    joined = " ".join(args)
    assert "-v /host/staging/config:/config:ro" in joined
    # The workspace lands at its own file address, so a path from write_file works
    # verbatim inside the container — and read-only, since inputs are not a
    # diagnostic's to rewrite.
    assert "-v /host/ws:/sources/ws1:ro" in joined
    assert ":/out" not in joined, "a diagnostic must never mount the results dir"


def test_no_workspace_mount_when_the_source_is_a_campaign():
    from robovast.service.docker_exec_lane import DockerExecLane
    spec = _spec()
    spec.config_dir = "/host/staging/config"
    joined = " ".join(DockerExecLane()._common_run_args(spec))
    assert "/sources/" not in joined


# -- lifetime: at most one container ----------------------------------------


def test_a_one_shot_leaves_nothing_behind():
    lane = FakeLane()
    mgr = ce.ContainerExecManager(lane)
    mgr.run(_spec(), 300, keep_alive=False, identity=("a",))
    assert mgr.state() is None
    assert not lane.alive


def test_a_one_shot_first_discards_a_held_container():
    # Otherwise "one-shot" would quietly inherit whatever the held container was left in.
    lane = FakeLane()
    mgr = ce.ContainerExecManager(lane)
    mgr.run(_spec(), 300, keep_alive=True, identity=("a",))
    mgr.run(_spec(), 300, keep_alive=False, identity=("a",))
    assert mgr.state() is None
    assert not lane.alive


def test_the_same_source_reuses_the_container():
    lane = FakeLane()
    mgr = ce.ContainerExecManager(lane)
    mgr.run(_spec(), 300, keep_alive=True, identity=("a",))
    assert mgr.state().reused is False
    mgr.run(_spec(), 300, keep_alive=True, identity=("a",))
    assert mgr.state().reused is True
    assert len(lane.starts) == 1


def test_a_different_source_replaces_an_idle_container_and_says_so():
    lane = FakeLane()
    mgr = ce.ContainerExecManager(lane)
    mgr.run(_spec(), 300, keep_alive=True, identity=("a",))
    mgr.run(_spec(), 300, keep_alive=True, identity=("b",))
    assert mgr.state().reused is False
    assert len(lane.starts) == 2


def test_a_different_source_is_refused_while_something_is_still_running():
    # Replacing would kill a scenario the caller deliberately started — a destructive
    # act inferred from a changed argument rather than asked for.
    lane = FakeLane()
    mgr = ce.ContainerExecManager(lane)
    mgr.run(_spec(), 300, keep_alive=True, identity=("a",))
    lane.busy = True
    with pytest.raises(ValueError, match="stop_container"):
        mgr.run(_spec(), 300, keep_alive=True, identity=("b",))
    assert len(lane.starts) == 1, "the live container must not have been replaced"


def test_a_scenario_is_detached_but_a_command_is_not():
    lane = FakeLane()
    mgr = ce.ContainerExecManager(lane)
    mgr.run(_spec(command=""), 300, keep_alive=True, identity=("a",))
    assert lane.execs[-1][2] is True
    mgr.run(_spec(command="ls"), 300, keep_alive=True, identity=("a",))
    assert lane.execs[-1][2] is False


def test_stopping_reports_truthfully_and_is_idempotent():
    lane = FakeLane()
    mgr = ce.ContainerExecManager(lane)
    mgr.run(_spec(), 300, keep_alive=True, identity=("a",))
    assert mgr.stop().stopped is True
    # "there was nothing to stop" is an empty result, not a failure.
    assert mgr.stop().stopped is False


def test_a_stray_container_is_stopped_even_without_a_record():
    # A container can outlive the record (a service restart); reaping it by name is why
    # the name is fixed.
    lane = FakeLane()
    lane.alive = True
    mgr = ce.ContainerExecManager(lane)
    assert mgr.stop().stopped is True


# -- the reaper's two clocks ------------------------------------------------


def test_an_idle_container_is_reaped(monkeypatch):
    monkeypatch.setattr(ce, "IDLE_REAP_S", 0.2)
    lane = FakeLane()
    mgr = ce.ContainerExecManager(lane, poll_s=0.05)
    mgr.run(_spec(), 300, keep_alive=True, identity=("a",))
    deadline = time.monotonic() + 5
    while mgr.state() is not None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert mgr.state() is None
    assert not lane.alive


def test_a_running_workload_is_not_idle_reaped(monkeypatch):
    # The bug this prevents: a scenario is running while the exec surface looks idle,
    # so an idle clock that ignored live processes would kill the work it was started for.
    monkeypatch.setattr(ce, "IDLE_REAP_S", 0.2)
    lane = FakeLane()
    lane.busy = True
    mgr = ce.ContainerExecManager(lane, poll_s=0.05)
    mgr.run(_spec(), 300, keep_alive=True, identity=("a",))
    time.sleep(0.6)
    assert mgr.state() is not None
    assert mgr.state().idle_expires_in_s is None, "no idle countdown while busy"


def test_the_hard_deadline_fires_even_with_a_running_workload():
    lane = FakeLane()
    lane.busy = True
    mgr = ce.ContainerExecManager(lane, poll_s=0.05)
    mgr.run(_spec(), 300, keep_alive=True, identity=("a",))
    mgr._held["deadline"] = time.monotonic() + 0.1
    deadline = time.monotonic() + 5
    while mgr.state() is not None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert mgr.state() is None


def test_an_unanswerable_probe_counts_as_busy_rather_than_reaping_a_live_run():
    class Unreachable(FakeLane):
        def held_workload_running(self):
            raise RuntimeError("docker unreachable")

    lane = Unreachable()
    mgr = ce.ContainerExecManager(lane, poll_s=0.05)
    mgr.run(_spec(), 300, keep_alive=True, identity=("a",))
    assert mgr.state().idle_expires_in_s is None


# -- staging ----------------------------------------------------------------


def _staged(config_name, command):
    return ce.stage(VAST, config_name, cluster=False, command=command)


def test_the_bare_image_case_stages_only_the_entrypoint():
    # Expanding a config tree to answer "does this import?" would put a seconds-long
    # check back on the variation-plugin path.
    spec, _data, _limit, _src = _staged("", "ls")
    try:
        assert sorted(os.listdir(spec.config_dir)) == ["entrypoint.sh"]
    finally:
        spec.close()


def test_a_staged_config_carries_the_full_config_set():
    # Omitting monitor_resources.py is not a crash but something worse:
    # start-stop-daemon --background forks fine, writes the pidfile, and the daemon dies
    # silently — a degradation the run would report as healthy.
    spec, _data, _limit, _src = _staged("minimal", "")
    try:
        staged = os.listdir(spec.config_dir)
        for required in ("entrypoint.sh", "monitor_resources.py", "collect_sysinfo.py",
                         "ros2_service.osc", "scenario.config"):
            assert required in staged, f"{required} missing from the staged /config"
    finally:
        spec.close()


def test_the_parameter_file_lands_where_the_entrypoint_already_looks():
    # A campaign stages a single config's parameters at <config>/_config/scenario.config,
    # which is the entrypoint's own default — so no SCENARIO_PARAMETER_FILE override.
    spec, _data, _limit, _src = _staged("minimal", "")
    try:
        assert os.path.isfile(os.path.join(spec.config_dir, "scenario.config"))
        assert "SCENARIO_PARAMETER_FILE" not in spec.env
    finally:
        spec.close()


def test_an_unknown_config_name_is_refused_with_the_available_names():
    from robovast.common.errors import CampaignConfigError
    with pytest.raises(CampaignConfigError, match="minimal"):
        _staged("no-such-config", "ls")


def test_the_entrypoint_is_rendered_for_the_lane_it_will_run_on():
    # A cluster campaign's entrypoint carries cluster init and S3-mirroring post-run
    # logic; running that locally would be wrong. This is why a campaign's own staged
    # entrypoint is never reused, only its config.
    local, _d, _l, _s = ce.stage(VAST, "", cluster=False, command="ls")
    cluster, _d2, _l2, _s2 = ce.stage(VAST, "", cluster=True, command="ls")
    try:
        local_text = open(os.path.join(local.config_dir, "entrypoint.sh")).read()
        cluster_text = open(os.path.join(cluster.config_dir, "entrypoint.sh")).read()
        assert local_text != cluster_text
        assert "@@" not in local_text and "@@" not in cluster_text
        assert "fixuid" in local_text, "the local lane's init block is missing"
    finally:
        local.close()
        cluster.close()


def test_a_scenario_run_reports_where_its_output_went():
    # entrypoint.sh redirects its own stdout when given no argv, so a started scenario's
    # output is a file in the container and `stdout` comes back near-empty. Returning the
    # path is what keeps that from looking like a broken tool.
    scenario, _d, _l, _s = _staged("minimal", "")
    command, _d2, _l2, _s2 = _staged("minimal", "ls")
    try:
        assert scenario.log_path.startswith(ce.OUTPUT_DIR)
        assert command.log_path == ""
    finally:
        scenario.close()
        command.close()


def test_staging_is_cleaned_up_and_survives_a_held_container():
    spec, _data, _limit, _src = _staged("minimal", "ls")
    staging = spec._staging_dir
    lane = FakeLane()
    mgr = ce.ContainerExecManager(lane)
    mgr.run(spec, 300, keep_alive=True, identity=("a",))
    # Still mounted as /config while the container is held.
    assert os.path.isdir(staging)
    mgr.stop()
    assert not os.path.exists(staging), "the held container's /config leaked"


def test_a_one_shot_cleans_up_its_own_staging():
    spec, _data, _limit, _src = _staged("minimal", "ls")
    staging = spec._staging_dir
    ce.ContainerExecManager(FakeLane()).run(spec, 300, keep_alive=False, identity=("a",))
    assert not os.path.exists(staging)


# -- the project's .vast, refusing ambiguity --------------------------------


def test_a_project_directory_with_several_vast_files_must_name_one(tmp_path):
    (tmp_path / "a.vast").write_text("version: 1\n")
    (tmp_path / "b.vast").write_text("version: 1\n")
    with pytest.raises(ValueError, match="several .vast files"):
        ce.vast_in_dir(str(tmp_path))
    assert ce.vast_in_dir(str(tmp_path), "a.vast").endswith("a.vast")


def test_a_project_directory_with_no_vast_file_is_an_error(tmp_path):
    with pytest.raises(ValueError, match="no .vast file"):
        ce.vast_in_dir(str(tmp_path))
