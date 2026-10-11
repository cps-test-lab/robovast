# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""basic_nav's notebooks are what ``build_notebooks.py`` writes, and read ground truth the same
way on both simulators.

The trajectory cell reads ``ground_truth_poses`` and nothing else, so the roqsim half and the
Gazebo half are one notebook: a run carrying roqsim's own recording and a run carrying only a
``*_gt`` frame on ``/tf`` both answer it.
"""

import importlib.util
import json
import pathlib

import pytest
import yaml

from robovast_data import open_data
from tests.robovast_data.conftest import write_store
from tests.robovast_decode.conftest import make_campaign, make_roqsim_campaign

EXAMPLE = pathlib.Path(__file__).resolve().parents[2] / "configs" / "examples" / "basic_nav"


def _builder():
    spec = importlib.util.spec_from_file_location(
        "basic_nav_build_notebooks", EXAMPLE / "analysis" / "build_notebooks.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_committed_notebooks_are_the_generated_ones():
    for name, notebook in _builder().NOTEBOOKS.items():
        committed = (EXAMPLE / "analysis" / name).read_text(encoding="utf-8")
        assert committed == json.dumps(notebook, indent=1, sort_keys=True) + "\n", (
            f"{name} differs from build_notebooks.py: regenerate it")


def test_the_trajectory_reads_ground_truth_and_no_frame_name():
    cell = _builder().POSES_IF_PRESENT
    assert "table('ground_truth_poses')" in cell
    assert "_gt" not in cell, "which frame is ground truth is the table's to know"


@pytest.mark.parametrize("simulator", ["roqsim", "gazebo"])
def test_both_halves_answer_the_trajectory_cell(tmp_path, simulator):
    campaign = tmp_path / "basic_nav-2026-01-01-00000000"
    if simulator == "roqsim":
        make_roqsim_campaign(campaign)
        (campaign / "_execution").mkdir()
        (campaign / "_execution" / "tables.yaml").write_text(yaml.safe_dump(
            {"groups": [], "ground_truth": {"table": "sim_poses", "entity_kind": "robot"}}))
    else:
        make_campaign(campaign)
    write_store(campaign, {"cfg": {"runs": {0: "passed"}}})

    poses = open_data(str(campaign / "cfg" / "0")).table("ground_truth_poses")
    assert not poses.empty
    assert poses["frame"].nunique() == 1
    assert {"position.x", "position.y", "config_name", "run_id"} <= set(poses.columns)
