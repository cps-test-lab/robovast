# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``behaviors.jsonl`` must land in the run directory, and be recorded by default.

Placement is not obvious. ``scenario_execution`` is given ``-o /out``, which is the
*campaign root*, not a run directory — where each run's results actually go is decided by
the ``_output_dir`` that :func:`build_job_parameter_documents` puts in every job document.
So a packed job writing several runs from one process is the case that can silently put the
file in the wrong place, and the assertion that catches it is not "the file exists" but
"the file sits beside the ``test.xml`` of the same run".

The tests drive the real derivation (``JobSpec``/``WorkItem`` →
``build_job_parameter_documents`` → ``dump_multi_document_yaml``) rather than a hand-written
parameter file, so they break if that derivation changes rather than testing a copy of it.
``build_job_parameter_documents`` decides the run directory for every run on both lanes and
had no test before this one.
"""

import json
import subprocess
import sys
from xml.etree import ElementTree

import pytest

from robovast.common.execution import (build_job_parameter_documents,
                                       dump_multi_document_yaml, scenario_env)
from robovast.execution.packer import JobSpec, WorkItem

pytest.importorskip("scenario_execution",
                    reason="the placement assertion needs the real runner")


def _runner_supports_bt_log() -> bool:
    """Whether the installed scenario_execution knows ``--bt-log``.

    Checked rather than assumed: an older runner *silently ignores* the unknown flag
    (both entry points use ``parse_known_args``), so without this the placement tests
    would fail with "no such file" and blame the placement rather than the runner.
    """
    from scenario_execution.scenario_execution_base import ScenarioExecution
    return any("--bt-log" in (a.option_strings or [])
               for a in ScenarioExecution.get_arg_parser()._actions)


needs_bt_log = pytest.mark.skipif(
    not _runner_supports_bt_log(),
    reason="installed scenario_execution predates --bt-log")

SCENARIO = """\
import osc.helpers

scenario demo:
    do serial:
        log("hello")
"""


def _item(name, run):
    return WorkItem(config={"name": name}, run_number=run)


def _run_scenario_execution(scenario_path, out_root, param_file):
    """Run the packed-job command line the entrypoint builds, with --bt-log."""
    result = subprocess.run(
        [sys.executable, "-m", "scenario_execution.scenario_execution_base",
         "-o", str(out_root), str(scenario_path),
         "--scenario-parameter-file", str(param_file),
         "--output-result-per-scenario", "--bt-log"],
        capture_output=True, text=True, timeout=300, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def _write_inputs(tmp_path, job):
    scenario = tmp_path / "scenario.osc"
    scenario.write_text(SCENARIO, encoding="utf-8")
    params = tmp_path / "job.params.yaml"
    params.write_text(
        dump_multi_document_yaml(build_job_parameter_documents(job, "demo")),
        encoding="utf-8")
    return scenario, params


def _plan():
    """The container plan the compose generator now takes; one container is the
    ordinary shape for a campaign with no simulator."""
    from robovast.common.containers import plan_containers
    return plan_containers({"containers": {"scenario": {"image": "img:test"}}})


def test_output_dir_is_per_run(tmp_path):
    """The documents place each item at <config>/<run> — relative to -o, not absolute."""
    job = JobSpec(items=[_item("cfg-a", 0), _item("cfg-a", 1), _item("cfg-b", 0)], index=0)
    docs = build_job_parameter_documents(job, "demo")
    assert [d["demo"]["_output_dir"] for d in docs] == ["cfg-a/0", "cfg-a/1", "cfg-b/0"]


@needs_bt_log
def test_behaviors_jsonl_lands_beside_test_xml_in_a_packed_job(tmp_path):
    """Three runs from one process, each file in its own run directory.

    ``-o`` is the campaign root here, exactly as in a packed job, so this fails if the
    log is ever written relative to the runner's output dir instead of the scenario's.
    """
    job = JobSpec(items=[_item("cfg-a", 0), _item("cfg-a", 1), _item("cfg-b", 0)], index=0)
    scenario, params = _write_inputs(tmp_path, job)
    out_root = tmp_path / "out"
    out_root.mkdir()

    _run_scenario_execution(scenario, out_root, params)

    seen = set()
    for config_name, run in (("cfg-a", 0), ("cfg-a", 1), ("cfg-b", 0)):
        run_dir = out_root / config_name / str(run)
        log = run_dir / "behaviors.jsonl"
        assert log.is_file(), f"no behaviours log in {run_dir}"
        # Beside the run sentinel, not merely somewhere under the campaign root: this is
        # what makes the ingest find it as *this* run's data.
        assert (run_dir / "test.xml").is_file()

        meta = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
        assert meta["format"] == "behaviour_tree_log"
        # The two files in this directory must describe the *same* run. Multi-document
        # runs suffix the scenario name per document (demo-0, demo-1, …), so this also
        # catches a log written into the wrong sibling directory — which merely
        # asserting that a file exists would not.
        testcase = ElementTree.parse(run_dir / "test.xml").find(".//testcase")
        assert meta["scenario"] == testcase.get("name")
        seen.add(meta["scenario"])

    assert len(seen) == 3, f"runs shared a log instead of one each: {seen}"
    # Nothing stray at the campaign root, which is where a mis-resolved path would land.
    assert not (out_root / "behaviors.jsonl").exists()


@needs_bt_log
def test_single_run_job_writes_into_its_run_directory(tmp_path):
    job = JobSpec(items=[_item("cfg-a", 0)], index=0)
    scenario, params = _write_inputs(tmp_path, job)
    out_root = tmp_path / "out"
    out_root.mkdir()

    _run_scenario_execution(scenario, out_root, params)

    run_dir = out_root / "cfg-a" / "0"
    assert (run_dir / "behaviors.jsonl").is_file()
    assert (run_dir / "test.xml").is_file()
    assert not (out_root / "behaviors.jsonl").exists()


def test_local_lane_compose_carries_the_flag(tmp_path):
    """The env the local lane derives reaches the compose file as a plain variable."""
    from robovast.execution.execution_utils.execute_local import _build_packed_compose_yaml

    campaign_data = {"execution": {"containers": {"scenario": {"image": "img:test"}}, "runs": 1},
                     "scenario_file": "scenario.osc"}
    yaml_text = _build_packed_compose_yaml(
        docker_image="img:test", out_path=str(tmp_path), results_dir_var="${RESULTS}",
        job=JobSpec(items=[_item("cfg-a", 0)], index=0), param_file_rel="p.yaml",
        run_files=[], env_vars={}, pre_command=None, post_command=None, uid=1000, gid=1000,
        main_cpu=1, main_memory=None, main_gpu=False, plan=_plan(),
        use_gui_block=False, scenario_env_vars=scenario_env(campaign_data))
    assert "- BT_LOG=true" in yaml_text


def test_local_lane_compose_states_an_opt_out(tmp_path):
    from robovast.execution.execution_utils.execute_local import _build_packed_compose_yaml

    campaign_data = {"execution": {"containers": {"scenario": {"image": "img:test"}}, "runs": 1, "bt_log": False},
                     "scenario_file": "scenario.osc"}
    yaml_text = _build_packed_compose_yaml(
        docker_image="img:test", out_path=str(tmp_path), results_dir_var="${RESULTS}",
        job=JobSpec(items=[_item("cfg-a", 0)], index=0), param_file_rel="p.yaml",
        run_files=[], env_vars={}, pre_command=None, post_command=None, uid=1000, gid=1000,
        main_cpu=1, main_memory=None, main_gpu=False, plan=_plan(),
        use_gui_block=False, scenario_env_vars=scenario_env(campaign_data))
    assert "- BT_LOG=false" in yaml_text
