# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""RoboVAST records the run: the entrypoint starts the scenario's bag recorder itself.

What a run records is a property of the campaign (its ``recording:`` block, arriving in the
container as the ``RECORD_*`` variables) and not of the scenario text. The entrypoint builds
the recorder line from those variables, starts it before the runner in a campaign job, which
names the run's directory, and the cleanup scripts close it -- INT, a bounded wait, KILL --
before the definitions are dumped. Both recorders write through and split, from one storage
preset that ships with the run scripts.

The argument builder and the start function are cut out of the *shipped* script and run
through a real bash, with a fake ``ros2`` on PATH where a recorder has to be started: what
matters is the argv the recorder receives, and no assertion on the script's text shows that.
"""

import os
import shutil
import subprocess
import textwrap
from importlib.resources import files

import pytest

from robovast.common import execution
from robovast.common.execution import MCAP_STORAGE_CONFIG, render_entrypoint

PRESET = "/config/mcap_writethrough.yaml"
WRITE_THROUGH = ["--storage", "mcap", "--max-cache-size", "0",
                 "--storage-config-file", PRESET, "-d", "10"]


def _recording_block() -> str:
    """The recorder functions as shipped, with the options line they read."""
    rendered = render_entrypoint(cluster=False)
    options = next(line for line in rendered.splitlines()
                   if line.lstrip().startswith("RECORD_OPTIONS="))
    start = rendered.index("    scenario_record_args() {")
    end = rendered.index('    if [ -z "${RUN_OUTPUT_DIR:-}" ]', start)
    return textwrap.dedent(options + "\n" + rendered[start:end])


def _record_args(env: dict) -> list:
    """The argv the entrypoint hands ``ros2`` for the scenario recorder under *env*."""
    script = "\n".join([
        "set -e", 'log() { echo "$*"; }', _recording_block(),
        "scenario_record_args", 'printf "%s\\n" "${SCENARIO_RECORD_ARGS[@]}"'])
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True,
                         env={"PATH": os.environ["PATH"], "RUN_OUTPUT_DIR": "/out/cell/0",
                              **env})
    return out.stdout.splitlines()


# Hidden topics are always included: an action's feedback and status topics are hidden.
HEAD = ["bag", "record", "-o", "/out/cell/0/rosbag2"] + WRITE_THROUGH + ["--include-hidden-topics"]


# -- the recorder line ------------------------------------------------------------------------

def test_absent_variables_record_everything_in_wall_time():
    assert _record_args({}) == HEAD + ["-a"]


def test_all_with_sim_time():
    assert _record_args({"RECORD_TOPICS": "all", "RECORD_USE_SIM_TIME": "true"}) == \
        HEAD + ["--use-sim-time", "-a"]


def test_use_sim_time_false_adds_nothing():
    assert _record_args({"RECORD_USE_SIM_TIME": "false"}) == HEAD + ["-a"]


def test_topic_names_go_to_topics():
    assert _record_args({"RECORD_TOPICS": "/odom /scan"}) == HEAD + ["--topics", "/odom", "/scan"]


def test_regexes_are_joined_into_one_pattern():
    """One ``-e`` pattern, and the regex reaches the recorder as it was written -- its
    parentheses, anchors and alternation are one argument, not words for the shell."""
    assert _record_args({"RECORD_TOPICS": "^/tf(_static)?$ ^/cmd_.*"}) == \
        HEAD + ["-e", "^/tf(_static)?$|^/cmd_.*"]


def test_names_and_regexes_together():
    assert _record_args({"RECORD_TOPICS": "/odom ^/tf.* /scan"}) == \
        HEAD + ["--topics", "/odom", "/scan", "-e", "^/tf.*"]


def test_an_exclude_regex():
    assert _record_args({"RECORD_EXCLUDE": "^/camera/|^/depth/"}) == \
        HEAD + ["-a", "--exclude-regex", "^/camera/|^/depth/"]


def test_excluded_types():
    assert _record_args({"RECORD_EXCLUDE_TYPES":
                         "sensor_msgs/msg/Image sensor_msgs/msg/PointCloud2"}) == \
        HEAD + ["-a", "--exclude-topic-types", "sensor_msgs/msg/Image",
                "sensor_msgs/msg/PointCloud2"]


def test_every_flag_at_once():
    assert _record_args({"RECORD_TOPICS": "/odom ^/tf.*", "RECORD_EXCLUDE": "^/tf_static",
                         "RECORD_EXCLUDE_TYPES": "sensor_msgs/msg/Image",
                         "RECORD_USE_SIM_TIME": "true"}) == \
        HEAD + ["--use-sim-time", "--topics", "/odom", "-e", "^/tf.*",
                "--exclude-regex", "^/tf_static", "--exclude-topic-types",
                "sensor_msgs/msg/Image"]


def test_empty_excludes_add_nothing():
    assert _record_args({"RECORD_EXCLUDE": "", "RECORD_EXCLUDE_TYPES": ""}) == HEAD + ["-a"]


# -- the infrastructure recorder ----------------------------------------------------------------

def test_the_infrastructure_recorder_writes_through_and_splits():
    rendered = render_entrypoint(cluster=True)
    line = next(l for l in rendered.splitlines() if "rosout_bag ${RECORD_OPTIONS}" in l)
    assert "exec ros2 bag record -o ${OUTPUT_DIR}/logs/rosout_bag ${RECORD_OPTIONS} --topics ${LOG_TOPICS}" in line
    assert f'RECORD_OPTIONS="{" ".join(WRITE_THROUGH)}"' in rendered


# -- starting it --------------------------------------------------------------------------------

needs_ssd = pytest.mark.skipif(shutil.which("start-stop-daemon") is None,
                               reason="the entrypoint starts daemons with start-stop-daemon")


def _fake_ros2(tmp_path, body: str) -> None:
    """A ``ros2`` on PATH that records its argv to ``ros2.argv`` and then runs *body*."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    (bindir / "ros2").write_text(
        "#!/bin/bash\n"
        f'printf "%s\\n" "$@" > "{tmp_path}/ros2.argv"\n'
        + body, encoding="utf-8")
    (bindir / "ros2").chmod(0o755)


def _start(tmp_path, env: dict) -> subprocess.CompletedProcess:
    """Run the shipped start function with the pidfile under *tmp_path*."""
    pidfile = tmp_path / "scenario_bag.pid"
    block = _recording_block().replace("/tmp/scenario_bag.pid", str(pidfile))
    script = "\n".join(["set -e", 'log() { echo "$*"; }', block, "start_scenario_recorder"])
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=False, timeout=60,
        env={"PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
             "RUN_OUTPUT_DIR": str(tmp_path / "out" / "cell" / "0"), **env})


def _stop(tmp_path) -> None:
    pidfile = tmp_path / "scenario_bag.pid"
    if pidfile.exists():
        subprocess.run(["start-stop-daemon", "--stop", "--signal", "TERM", "--pidfile",
                        str(pidfile), "--retry", "TERM/5/KILL/1"], check=False)


@needs_ssd
def test_the_recorder_is_started_with_the_line_built_from_the_environment(tmp_path):
    """The fake opens the bag directory the way rosbag2 does, and stays: the entrypoint must
    see it open, log where it records, and go on to the runner."""
    # The daemon keeps the entrypoint's stdio (that is how its output reaches the run log),
    # which here is the test's capture pipe; the fake lets go of it so the entrypoint's
    # shell can be waited for while the recorder stays up.
    _fake_ros2(tmp_path, f'exec > "{tmp_path}/ros2.out" 2>&1\n'
                         'out=""; while [ $# -gt 0 ]; do [ "$1" = -o ] && out="$2"; shift; done\n'
                         'trap "exit 0" TERM INT\n'
                         'mkdir -p "$out"\n'
                         'sleep 30 & wait $!\n')
    try:
        out = _start(tmp_path, {"RECORD_TOPICS": "/odom ^/tf.*", "RECORD_USE_SIM_TIME": "true"})
        assert out.returncode == 0, out.stdout + out.stderr
        argv = (tmp_path / "ros2.argv").read_text().splitlines()
        run_dir = str(tmp_path / "out" / "cell" / "0")
        assert argv == ["bag", "record", "-o", f"{run_dir}/rosbag2"] + WRITE_THROUGH + \
            ["--include-hidden-topics", "--use-sim-time", "--topics", "/odom", "-e", "^/tf.*"]
        assert (tmp_path / "out" / "cell" / "0" / "rosbag2").is_dir()
        assert f"Scenario recording -> {run_dir}/rosbag2 (PID=" in out.stdout
        pid = int((tmp_path / "scenario_bag.pid").read_text())
        os.kill(pid, 0)  # still recording: the entrypoint left it running
    finally:
        _stop(tmp_path)


@needs_ssd
def test_a_recorder_that_exits_before_opening_its_bag_fails_the_run(tmp_path):
    """A daemon that refused its arguments would otherwise leave a run that looks fine and has
    no bag. Its complaint reaches the log (the daemon keeps the entrypoint's stdio)."""
    _fake_ros2(tmp_path, 'echo "ros2 bag record: error: unrecognized arguments" >&2\nexit 2\n')
    out = _start(tmp_path, {})
    assert out.returncode == 1
    assert "ERROR: The scenario recorder (PID=" in out.stdout
    assert "exited before it opened" in out.stdout
    assert "unrecognized arguments" in out.stdout + out.stderr


@pytest.mark.parametrize("cluster", [False, True])
def test_a_container_with_no_run_directory_starts_no_scenario_recorder_and_says_so(cluster):
    rendered = render_entrypoint(cluster=cluster)
    block = rendered[rendered.index('    if [ -z "${RUN_OUTPUT_DIR:-}" ]'):]
    block = block[:block.index("    fi\n") + len("    fi\n")]
    assert 'log "No scenario recording: no run directory (RUN_OUTPUT_DIR)' in block
    assert "start_scenario_recorder" in block
    assert rendered.index("start_scenario_recorder\n") < rendered.index("run_scenario ${RUNNER_CMD}"), \
        "the recorder is started before the runner"


# -- stopping it: the cleanup scripts, in a pod and outside one ------------------------------------

@pytest.mark.parametrize("cluster", [False, True])
def test_the_cleanup_stops_the_scenario_recorder_before_the_definitions_dump(cluster):
    rendered = render_entrypoint(cluster=cluster)
    cleanup = rendered[rendered.index("CLEANUP_EOF'") + len("CLEANUP_EOF'"):]
    cleanup = cleanup[:cleanup.index("\nCLEANUP_EOF")]
    scenario_stop = cleanup.index("/tmp/scenario_bag.pid")
    infra_stop = cleanup.index("/tmp/rosbag.pid")
    dump = cleanup.index("dump_message_definitions.py")
    monitor = cleanup.index("/tmp/monitor.pid")
    assert scenario_stop < infra_stop < dump < monitor


def test_the_cleanup_outside_a_pod_gives_a_recorder_a_bounded_wait_then_kills():
    """INT, up to 30 s for the close, KILL: the same shape as the pod's retry schedule, so a
    bag that closes slowly is closed rather than killed with no metadata."""
    rendered = render_entrypoint(cluster=False)
    helper = rendered[rendered.index("_stop_recorder() {"):rendered.index('_stop_recorder "scenario_bag"')]
    assert "--signal INT" in helper
    assert "-lt 300" in helper
    assert "kill -KILL" in helper


def test_the_cluster_cleanup_uses_the_retry_schedule_for_both_recorders():
    rendered = render_entrypoint(cluster=True)
    assert '_stop_daemon "scenario_bag" "/tmp/scenario_bag.pid" "INT" "INT/30/KILL/5"' in rendered
    assert '_stop_daemon "rosbag" "/tmp/rosbag.pid" "INT" "INT/30/KILL/5"' in rendered


# -- the storage preset ---------------------------------------------------------------------------

def test_the_preset_is_write_through_and_ships_with_the_run_scripts():
    preset = files("robovast.execution.data").joinpath(MCAP_STORAGE_CONFIG)
    assert preset.is_file()
    settings = [l for l in preset.read_text(encoding="utf-8").splitlines()
                if l.strip() and not l.lstrip().startswith("#")]
    assert settings == ["noChunking: true"]
    assert MCAP_STORAGE_CONFIG in execution.RESERVED_CONFIG_MOUNT_NAMES
    assert PRESET == f"/config/{MCAP_STORAGE_CONFIG}"


@pytest.mark.parametrize("cluster", [False, True])
def test_a_campaign_stages_the_preset_into_its_transient_dir(tmp_path, cluster):
    (tmp_path / "s.vast").write_text("version: 5\n", encoding="utf-8")
    (tmp_path / "s.osc").write_text("scenario x:\n    do serial:\n        wait elapsed(1s)\n",
                                    encoding="utf-8")
    out = tmp_path / "campaign"
    execution.prepare_campaign_configs(str(out), {
        "vast": str(tmp_path / "s.vast"), "scenario_file": str(tmp_path / "s.osc"),
        "configs": [{"name": "c1", "config": {}}], "execution": {"runs": 1}}, cluster=cluster)
    staged = out / "_transient" / MCAP_STORAGE_CONFIG
    assert staged.read_bytes() == \
        files("robovast.execution.data").joinpath(MCAP_STORAGE_CONFIG).read_bytes()
