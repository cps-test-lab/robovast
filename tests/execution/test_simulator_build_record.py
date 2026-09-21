# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Every run records which build of the simulator it ran.

An image digest names bytes, and a build lock lists distributions by version -- which for a
simulator released as ``0.1.0`` between two releases is every build alike. So each run's own
container runs the backend's version command and records what it printed, and postprocessing
has the backend read that back into ``_execution/simulator_build.yaml``.

What this file holds, driven by a stub backend (the core suite imports no simulator):

- the backend's command reaches the run's containers through its environment;
- a container records it only when it has the command, verbatim, and never fails the run;
- the record is per run; a run whose image names no build records that as absent, with the
  reason, and a campaign with no record at all leaves the file absent (unknown).
"""

import json
import sys
from importlib.resources import files

import pytest

import robovast.execution.data.collect_sysinfo as collector
from robovast.common.campaign_data import read_simulator_build_record
from robovast.common.execution import scenario_env, sidecar_backend_env
from robovast.common.simulators import (SHAPE_ROS, SIMULATOR_VERSION_ENV, SimulatorBackend,
                                        apply_backend, shape_for)
from robovast.results_processing import postprocessing
from robovast.results_processing.postprocessing import _record_simulator_builds

SHA = "0123456789abcdef0123456789abcdef01234567"


class VersionedBackend(SimulatorBackend):
    """A simulator that names its build on ``stubsim --version``."""

    VERSION_COMMAND = ("stubsim", "--version")

    def containers(self, cfg, execution):
        if shape_for(execution.get("mode")) == SHAPE_ROS:
            return {"simulation": {"image": "vendor/sim:1", "command": ["stubsim"]}}
        return {"scenario": {"image": "combined/sim:1"}}

    def parse_version(self, output):
        line = output.strip()
        if ", build " in line:
            return {"version": "1.0", "build": line.rsplit(" ", 1)[-1]}
        return {"version": "1.0", "build": None, "absent": "this build names none"}


class SilentBackend(VersionedBackend):
    VERSION_COMMAND = ()


@pytest.fixture(autouse=True)
def _register(monkeypatch):
    import robovast.common.simulators as mod
    backends = {"versioned": VersionedBackend, "silent": SilentBackend}
    monkeypatch.setattr(mod, "resolve_backend", lambda name, base_dir="": backends[name]())


def _execution(backend="versioned", mode="base"):
    return {"mode": mode, "runs": 1, "containers": {"simulation": {"backend": backend}}}


# -- the command reaches the container ---------------------------------------------------


def test_the_version_command_reaches_the_simulator_in_either_shape():
    stepped = apply_backend(_execution(mode="base"))
    assert scenario_env({"execution": stepped})[SIMULATOR_VERSION_ENV] == "stubsim --version"
    ros = apply_backend(_execution(mode="ros2"))
    assert sidecar_backend_env(ros, "simulation")[SIMULATOR_VERSION_ENV] == "stubsim --version"


def test_a_backend_without_a_version_command_sends_none():
    assert SIMULATOR_VERSION_ENV not in scenario_env(
        {"execution": apply_backend(_execution("silent"))})


def test_the_container_script_spells_the_variable_the_core_sends():
    """The script runs in images that do not carry robovast, so it cannot import the name."""
    assert collector.SIMULATOR_VERSION_ENV == SIMULATOR_VERSION_ENV


@pytest.mark.parametrize("name,path", [
    ("entrypoint.sh", "simulator_version_main.json"),
    ("secondary_entrypoint.sh", "simulator_version_${CONTAINER_NAME}.json"),
])
def test_both_entrypoints_record_it_per_container(name, path):
    script = files("robovast.execution.data").joinpath(name).read_text(encoding="utf-8")
    assert f'--simulator-version "${{OUTPUT_DIR}}/{path}"' in script


# -- what a container records ------------------------------------------------------------


def test_a_container_with_the_command_records_what_it_printed(tmp_path, monkeypatch):
    command = f"{sys.executable} -c \"print('stubsim, version 1.0, build {SHA}')\""
    monkeypatch.setenv(collector.SIMULATOR_VERSION_ENV, command)
    target = tmp_path / "simulator_version_main.json"
    collector.write_simulator_version(str(target))
    record = json.loads(target.read_text())
    assert record["exit_code"] == 0
    assert record["stdout"].strip() == f"stubsim, version 1.0, build {SHA}"
    assert record["command"] == command


def test_a_failing_command_is_recorded_with_its_exit_code(tmp_path, monkeypatch):
    monkeypatch.setenv(collector.SIMULATOR_VERSION_ENV,
                       f"{sys.executable} -c \"import sys; sys.exit('no such flag')\"")
    target = tmp_path / "v.json"
    collector.write_simulator_version(str(target))
    record = json.loads(target.read_text())
    assert record["exit_code"] == 1
    assert "no such flag" in record["stderr"]


def test_a_container_without_the_command_records_nothing(tmp_path, monkeypatch):
    """In the ROS shape the scenario container has no simulator, and a record from it saying so
    would read like a finding about the run."""
    monkeypatch.setenv(collector.SIMULATOR_VERSION_ENV, "surely-not-installed-here --version")
    target = tmp_path / "v.json"
    collector.write_simulator_version(str(target))
    assert not target.exists()


def test_no_command_named_records_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv(collector.SIMULATOR_VERSION_ENV, raising=False)
    target = tmp_path / "v.json"
    collector.write_simulator_version(str(target))
    assert not target.exists()


# -- what postprocessing makes of it -----------------------------------------------------


def _campaign(tmp_path, jobs, backend="versioned"):
    """``jobs``: ``{job name: {container: record}}``, laid out as ``_jobs/batch-0/<job>/``."""
    root = tmp_path / "camp"
    for job, records in jobs.items():
        job_dir = root / "_jobs" / "batch-0" / job
        job_dir.mkdir(parents=True)
        for container, record in records.items():
            (job_dir / f"simulator_version_{container}.json").write_text(
                record if isinstance(record, str) else json.dumps(record))
    (root / "_config").mkdir(parents=True, exist_ok=True)
    (root / "_config" / "camp.vast").write_text(
        f"execution:\n  containers:\n    simulation:\n      backend: {backend}\n")
    return root


def _ok(line):
    return {"command": "stubsim --version", "exit_code": 0, "stdout": line + "\n", "stderr": ""}


def _run(root):
    lines = []
    _record_simulator_builds(root, lines.append)
    return read_simulator_build_record(root), lines


def test_each_run_records_the_build_its_image_named(tmp_path):
    root = _campaign(tmp_path, {
        "job-0": {"simulation": _ok(f"stubsim, version 1.0, build {SHA}")},
        "job-1": {"simulation": _ok("stubsim, version 1.0")},
    })
    record, lines = _run(root)
    assert record["command"] == "stubsim --version"
    assert record["runs"]["_jobs/batch-0/job-0"] == {
        "container": "simulation", "version": "1.0", "build": SHA}
    older = record["runs"]["_jobs/batch-0/job-1"]
    assert older["build"] is None, "an image that names no build is not given one"
    assert older["absent"] == "this build names none"
    assert "1 with none recorded" in lines[-1]


def test_a_run_that_recorded_nothing_is_named_rather_than_left_out(tmp_path):
    """Counting the records that exist would report a campaign as fully recorded while it is
    short of runs -- a container that never ran the command writes no file at all."""
    root = _campaign(tmp_path, {"job-0": {"simulation": _ok(f"stubsim, version 1.0, build {SHA}")}})
    (root / "cfg" / "0").mkdir(parents=True)
    (root / "cfg" / "1").mkdir(parents=True)
    (root / "_jobs" / "batch-0" / "job-1").mkdir(parents=True)
    (root / "_transient").mkdir(parents=True, exist_ok=True)
    (root / "_transient" / "job_links.yaml").write_text(
        "cfg/0/job: ../../_jobs/batch-0/job-0\ncfg/1/job: ../../_jobs/batch-0/job-1\n")

    record, lines = _run(root)

    assert record["runs"]["_jobs/batch-0/job-1"] == {
        "build": None, "absent": "no container of this job recorded a version"}
    assert "2 run(s)" in lines[-1] and "1 with none recorded" in lines[-1]


def test_a_run_whose_command_failed_is_absent_with_the_reason(tmp_path):
    root = _campaign(tmp_path, {"job-0": {"main": {
        "command": "stubsim --version", "exit_code": 2, "stdout": "",
        "stderr": "error: no such option: --version"}}})
    record, _ = _run(root)
    run = record["runs"]["_jobs/batch-0/job-0"]
    assert run["build"] is None
    assert "failed in this image" in run["absent"]
    assert "no such option" in run["absent"]


def test_an_unreadable_record_is_absent_not_skipped(tmp_path):
    root = _campaign(tmp_path, {"job-0": {"main": "{not json"}})
    record, _ = _run(root)
    assert "could not be read" in record["runs"]["_jobs/batch-0/job-0"]["absent"]


def test_two_containers_answering_are_both_kept(tmp_path):
    root = _campaign(tmp_path, {"job-0": {
        "main": _ok(f"stubsim, version 1.0, build {SHA}"),
        "simulation": _ok(f"stubsim, version 1.0, build {'f' * 40}")}})
    record, _ = _run(root)
    containers = record["runs"]["_jobs/batch-0/job-0"]["containers"]
    assert {c["container"] for c in containers} == {"main", "simulation"}


def test_no_record_from_any_run_leaves_the_file_absent(tmp_path):
    """Absent is "unknown" -- runs that predate the record. Writing an empty one would claim
    the runs were asked."""
    root = _campaign(tmp_path, {})
    record, lines = _run(root)
    assert record is None
    assert "leaving the record absent" in lines[-1]


def test_a_backend_without_a_version_command_writes_nothing(tmp_path):
    root = _campaign(tmp_path, {"job-0": {"main": _ok("anything")}}, backend="silent")
    record, lines = _run(root)
    assert record is None
    assert lines == []


def test_a_failure_while_recording_is_reported_not_raised(tmp_path, monkeypatch):
    root = _campaign(tmp_path, {"job-0": {"main": _ok(f"stubsim, version 1.0, build {SHA}")}})

    def _boom(*_a, **_k):
        raise OSError("read-only campaign dir")

    monkeypatch.setattr("robovast.common.campaign_data.write_simulator_build_record", _boom)
    lines = []
    postprocessing._record_simulator_builds(root, lines.append)
    assert "could not record the simulator's build" in lines[-1]
