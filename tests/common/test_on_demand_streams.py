# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A recording of every topic leaves out the simulator's on-demand streams.

A simulator that renders a camera image only while something subscribes spends nothing on it
until a subscriber appears, and a recorder of every topic is one. So which streams are on
demand is asked of the simulator when a campaign is composed, frozen into each configuration,
recorded beside it, and handed to the run's recorder as exact-match excludes. A campaign that
names a stream records it.

Driven by a stub backend: RoboVAST's own suite never imports a simulator.
"""

import json
import os
import subprocess
import textwrap

import pytest
from pydantic import BaseModel, ConfigDict

from robovast.common import config_generation
from robovast.common.config_generation import _resolve_config_sim_blocks
from robovast.common.errors import CampaignConfigError
from robovast.common.execution import recording_env, recording_left_out
from robovast.common.variation.container_runner import ContainerSpec
from robovast.common.simulators import (ContainerQuery, SimulatorBackend, checked_streams,
                                        declares_on_demand_streams, sim_on_demand_streams)

CAMERA = {"topic": "/oakd/rgb/preview/image_raw", "type": "sensor_msgs/msg/Image"}
DEPTH = {"topic": "/oakd/rgb/preview/depth", "type": "sensor_msgs/msg/Image"}


class StageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stage: str


class SilentBackend(SimulatorBackend):
    """Says nothing about its streams: the default."""
    CONFIG_CLASS = StageConfig


class QueryBackend(SimulatorBackend):
    """Has to load its world to know, so it asks its image, naming the stage."""
    CONFIG_CLASS = StageConfig

    def on_demand_streams(self, cfg, execution):
        return ContainerQuery(ContainerSpec(image="vendor/sim:1"), ["sim", "on-demand", cfg.stage])


class _Runner:
    """The simulator's image, answering the on-demand query per stage."""

    def __init__(self, replies, asked):
        self._replies = replies
        self._asked = asked

    def run(self, command, emit):
        self._asked.append(list(command))
        emit("some progress line")
        emit(json.dumps(self._replies[command[-1]]))

    def image_digest(self):
        return "sha256:" + "0" * 64

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _register(monkeypatch):
    import robovast.common.simulators as mod
    backends = {"silent": SilentBackend, "query": QueryBackend}
    monkeypatch.setattr(mod, "resolve_backend", lambda name, base_dir="": backends[name]())


@pytest.fixture
def asked(monkeypatch):
    """Install the image's answer for composition; hand back what it was asked."""
    calls: list = []
    replies = {"depot": {"on_demand": [DEPTH, CAMERA]}, "empty": {"on_demand": []},
               "broken": {"inputs": []}}
    monkeypatch.setattr(config_generation, "_make_container_runner",
                        lambda _spec, **_kw: _Runner(replies, calls))
    return calls


def _parameters(backend="query", recording=None, panels=None):
    execution = {"mode": "ros2", "runs": 1,
                 "containers": {"simulation": {"backend": backend, "stage": "depot"}}}
    if recording is not None:
        execution["recording"] = recording
    parameters = {"execution": execution}
    if panels:
        parameters["visualization"] = {"results": {"run_view": {"panels": panels}}}
    return parameters


def _compose(parameters, configs, tmp_path):
    _resolve_config_sim_blocks(configs, parameters, str(tmp_path), [])
    return configs


# -- the hook ----------------------------------------------------------------------------

def test_a_backend_that_cannot_say_leaves_nothing_out():
    execution = _parameters("silent")["execution"]
    assert SimulatorBackend().on_demand_streams(None, execution) is None
    assert sim_on_demand_streams(execution, {"backend": "silent", "stage": "x"}) is None
    assert declares_on_demand_streams(execution, {"backend": "silent", "stage": "x"}) is False


def test_a_query_with_nothing_to_run_it_is_refused_rather_than_skipped():
    execution = _parameters()["execution"]
    with pytest.raises(RuntimeError, match="nothing here can run one"):
        sim_on_demand_streams(execution, {"backend": "query", "stage": "depot"})


def test_the_reply_is_checked_and_sorted():
    assert checked_streams([CAMERA, DEPTH, CAMERA], "x") == [DEPTH, CAMERA]
    with pytest.raises(ValueError, match="does not list the on-demand streams"):
        SimulatorBackend().on_demand_from_reply(None, {}, {"inputs": []})
    # A relative name would not match the topic the recorder sees, so the stream would be
    # recorded while the record says it was left out.
    with pytest.raises(ValueError, match="fully qualified"):
        checked_streams([{"topic": "oakd/rgb/image_raw", "type": ""}], "x")


# -- composition freezes the answer into each configuration --------------------------------

def test_composition_freezes_the_left_out_streams_per_configuration(tmp_path, asked):
    configs = _compose(_parameters(), [{"name": "a"}, {"name": "b"}], tmp_path)
    for config in configs:
        assert config["recording"] == {"ros2": {"left_out": [DEPTH, CAMERA]}}
    # One distinct sim block, one question.
    assert asked == [["sim", "on-demand", "depot"]]


def test_a_campaign_that_names_its_topics_is_not_asked(tmp_path, asked):
    configs = _compose(_parameters(recording={"ros2": {"topics": ["/odom", "^/oakd/"]}}),
                       [{"name": "a"}], tmp_path)
    assert "recording" not in configs[0]
    assert not asked


def test_a_stream_the_results_side_names_is_kept(tmp_path, asked):
    panels = [{"camera": {"topic": CAMERA["topic"]}}]
    configs = _compose(_parameters(panels=panels), [{"name": "a"}], tmp_path)
    assert configs[0]["recording"]["ros2"]["left_out"] == [DEPTH]


def test_an_unanswerable_query_fails_composition(tmp_path, asked):
    parameters = _parameters()
    parameters["execution"]["containers"]["simulation"]["stage"] = "broken"
    with pytest.raises(ValueError, match="could not say which streams it computes on demand"):
        _compose(parameters, [{"name": "a"}], tmp_path)


def test_a_backend_that_cannot_say_is_not_asked(tmp_path, asked):
    configs = _compose(_parameters("silent"), [{"name": "a"}], tmp_path)
    assert "recording" not in configs[0]
    assert not asked


# -- the query runs where the simulator runs, and a failed check says why ----------------

#: ``roqsim check --json`` over a world whose plugins are not installed where it ran: the report on
#: stdout, spread over lines, and the exit status of a verdict that failed.
FAILED_REPORT = json.dumps({
    "ok": False, "reached": "resolve", "world": {},
    "problems": [{"stage": "config",
                  "message": "unknown plugin 'ros2_bridge' (known: floorplan, robot)"}],
}, indent=2)


def test_the_query_runs_in_the_environment_a_run_has(monkeypatch):
    from robovast.common.execution import run_env_prefix

    specs = []

    def runner(spec, **_kw):
        specs.append(spec)
        return _Runner({"depot": {"on_demand": [CAMERA]}}, [])

    monkeypatch.setattr(config_generation, "_make_container_runner", runner)
    query = QueryBackend().on_demand_streams(StageConfig(stage="depot"), {})
    config_generation._run_json_query(query, "/tmp", what="the query")
    assert specs[0].image == "vendor/sim:1"
    assert specs[0].command_prefix == run_env_prefix()


def test_a_local_runner_starts_the_query_through_the_run_environment(monkeypatch, tmp_path):
    """The argv ``docker run`` gets, and what that argv does when a shell runs it."""
    from robovast.common.execution import RUN_ENV_NOTE
    from robovast.common.variation import container_runner

    started = []
    monkeypatch.setattr(container_runner, "run_with_live_output",
                        lambda cmd, *_a, **_kw: started.append(cmd))
    spec = config_generation._query_spec(ContainerSpec(image="vendor/sim:1"))
    command = ["printf", "%s\n", "/config/world one.yaml", "--json"]
    container_runner.LocalContainerRunner(spec).run(command)
    docker = started[0]
    image = docker.index("vendor/sim:1")
    assert docker[docker.index("--entrypoint") + 1] == "/bin/bash"
    in_image = ["/bin/bash"] + docker[image + 1:]
    assert in_image[-len(command):] == command
    # The words reach the command unsplit, and the environment step says what it did on
    # stderr, apart from the stdout a caller parses.
    ran = subprocess.run(in_image, capture_output=True, text=True, check=True,
                         env={"PATH": os.environ["PATH"], "ROS_DISTRO": "absent"})
    assert ran.stdout.splitlines() == ["/config/world one.yaml", "--json"]
    assert f"{RUN_ENV_NOTE} ROS_DISTRO=absent but /opt/ros/absent/setup.bash is missing" \
        in ran.stderr


class _FailedCheck:
    """An image whose check of the world fails: the report, then a non-zero exit."""

    def __init__(self, *, through_callback):
        self._through_callback = through_callback

    def run(self, command, emit):
        if self._through_callback:
            for line in FAILED_REPORT.splitlines():
                emit(line)
            raise subprocess.CalledProcessError(5, command)
        from robovast.service.world_query import QueryCommandFailed
        raise QueryCommandFailed(5, command, output=FAILED_REPORT + "\n")

    def close(self):
        pass


@pytest.mark.parametrize("through_callback", [True, False])
def test_a_failed_check_is_refused_with_the_problem_its_report_states(monkeypatch,
                                                                     through_callback):
    monkeypatch.setattr(config_generation, "_make_container_runner",
                        lambda _spec, **_kw: _FailedCheck(through_callback=through_callback))
    query = QueryBackend().on_demand_streams(StageConfig(stage="depot"), {})
    with pytest.raises(RuntimeError,
                       match=r"the query failed in vendor/sim:1: config: unknown plugin "
                             r"'ros2_bridge'"):
        config_generation._run_json_query(query, "/tmp", what="the query")


# -- the run's recorder --------------------------------------------------------------------

def test_the_job_is_handed_the_frozen_topics():
    execution = _parameters()["execution"]
    config = {"name": "a", "sim": {"backend": "query", "stage": "depot"},
              "recording": {"ros2": {"left_out": [DEPTH, CAMERA]}}}
    assert recording_env(execution, config) == {
        "RECORD_LEFT_OUT": f"{DEPTH['topic']} {CAMERA['topic']}"}


def test_explicit_topics_leave_nothing_out():
    execution = _parameters(recording={"ros2": {"topics": ["/odom"]}})["execution"]
    config = {"name": "a", "sim": {"backend": "query", "stage": "depot"}}
    assert recording_env(execution, config) == {"RECORD_LEFT_OUT": ""}


def test_a_configuration_composed_without_asking_is_refused():
    execution = _parameters()["execution"]
    config = {"name": "a", "sim": {"backend": "query", "stage": "depot"}}
    with pytest.raises(CampaignConfigError, match="composed without asking"):
        recording_left_out(execution, config)
    # A backend that cannot say has nothing to have asked.
    silent = {"name": "a", "sim": {"backend": "silent", "stage": "depot"}}
    assert recording_left_out(_parameters("silent")["execution"], silent) == []


def _record_args(env: dict) -> list:
    from robovast.common.execution import render_entrypoint
    rendered = render_entrypoint(cluster=False)
    options = next(line for line in rendered.splitlines()
                   if line.lstrip().startswith("RECORD_OPTIONS="))
    start = rendered.index("    scenario_record_args() {")
    end = rendered.index("    start_scenario_recorder() {", start)
    script = "\n".join(["set -e", textwrap.dedent(options + "\n" + rendered[start:end]),
                        "scenario_record_args",
                        'printf "%s\\n" "${SCENARIO_RECORD_ARGS[@]}"'])
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True,
                         env={"PATH": os.environ["PATH"], "RUN_OUTPUT_DIR": "/out/c/0", **env})
    return out.stdout.splitlines()


def test_the_recorder_excludes_each_left_out_topic_by_exact_match():
    args = _record_args({"RECORD_LEFT_OUT": "/oakd/rgb/preview/depth /oakd/rgb/preview/image_raw"})
    assert "-a" in args
    pattern = args[args.index("--exclude-regex") + 1]
    assert pattern == "^(/oakd/rgb/preview/depth|/oakd/rgb/preview/image_raw)$"


def test_the_campaigns_own_exclude_still_applies():
    args = _record_args({"RECORD_EXCLUDE": "^/tf_static|_raw$",
                         "RECORD_LEFT_OUT": "/oakd/rgb/preview/depth"})
    assert args[args.index("--exclude-regex") + 1] == \
        "(^/tf_static|_raw$)|^(/oakd/rgb/preview/depth)$"
    assert args.count("--exclude-regex") == 1
