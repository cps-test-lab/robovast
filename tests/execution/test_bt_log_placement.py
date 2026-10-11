# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``behaviors.jsonl`` must land in the run directory, and be recorded by default.

Placement is not obvious. ``scenario_execution`` is given ``-o /out``, which is the
*campaign root*, not a run directory — where each run's results actually go is decided by
the ``_output_dir`` that :func:`build_job_parameter_documents` puts in the job's document.
So the assertion that catches a misplaced file is not "the file exists" but "the file sits
beside the ``test.xml`` of the run", and nothing strays to the campaign root.

The tests drive the real derivation (``Job`` →
``build_job_parameter_documents`` → ``dump_multi_document_yaml``) rather than a hand-written
parameter file, so they break if that derivation changes rather than testing a copy of it.
``build_job_parameter_documents`` decides the run directory.
"""

import json
import subprocess
import sys
from xml.etree import ElementTree

import pytest

from robovast.common.execution import build_job_parameter_documents, dump_multi_document_yaml
from robovast.execution.jobs import Job
from robovast_decode.authored import JSONL_READERS as _JSONL_READERS

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


def _job(name, run):
    return Job(config={"name": name}, run_number=run, index=0)


def _run_scenario_execution(scenario_path, out_root, param_file):
    """Run the command line the entrypoint builds, with --bt-log."""
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


def test_output_dir_is_the_runs(tmp_path):
    """The document places the run at <config>/<run> — relative to -o, not absolute."""
    docs = build_job_parameter_documents(_job("cfg-a", 1), "demo")
    assert [d["demo"]["_output_dir"] for d in docs] == ["cfg-a/1"]


@needs_bt_log
def test_the_job_writes_into_its_run_directory(tmp_path):
    job = _job("cfg-a", 0)
    scenario, params = _write_inputs(tmp_path, job)
    out_root = tmp_path / "out"
    out_root.mkdir()

    _run_scenario_execution(scenario, out_root, params)

    run_dir = out_root / "cfg-a" / "0"
    log = run_dir / "behaviors.jsonl"
    assert log.is_file(), f"no behaviours log in {run_dir}"
    # Beside the run sentinel, not merely somewhere under the campaign root: this is
    # what makes the ingest find it as *this* run's data.
    assert (run_dir / "test.xml").is_file()

    meta = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    # Not a literal spelling: scenario-execution renamed this format from
    # "behaviour_tree_log" to "behavior_tree_log", so which one arrives depends on
    # the image the run used, and pinning either makes this test fail on half the
    # images for no reason. What actually matters is that the ingest dispatches on
    # whatever was written -- an unrecognised format yields no rows and a silently
    # empty `behaviors` table, which is the failure worth catching.
    assert meta["format"] in _JSONL_READERS, (
        f"{meta['format']!r} is not a format postprocessing can read; "
        f"the behaviors table would come out empty")
    # The two files in this directory must describe the same run.
    testcase = ElementTree.parse(run_dir / "test.xml").find(".//testcase")
    assert meta["scenario"] == testcase.get("name")
    # Nothing stray at the campaign root, which is where a mis-resolved path would land.
    assert not (out_root / "behaviors.jsonl").exists()
