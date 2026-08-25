# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""A world-varying campaign, from ``.vast`` to what each job actually starts.

The unit tests pin the pieces; this pins the seam between them -- that a configuration's
resolved world survives composition, staging, packing and manifest rendering, and arrives
in the container it belongs to.
"""

import os
import textwrap

import pytest
import yaml

from robovast.common.config_generation import generate_scenario_variations
from robovast.common.execution import prepare_campaign_configs
from robovast.common.simulators import SIM_CONFIG_FILE, SIM_OVERRIDES_MOUNT

pytest.importorskip("robovast_sim_roqsim",
                    reason="the roqsim backend extra is not installed")

_SCENARIO = """\
import osc.robotics

scenario nav:
    goal_pose: pose_3d = pose_3d()
    do serial:
        wait elapsed(1s)
"""

_WORLD = "sim: {pacing: realtime}\n"



#: Every test here drives a simulator backend end to end.
pytestmark = pytest.mark.requires_simulator

def _project(tmp_path, configuration):
    (tmp_path / "scenario.osc").write_text(_SCENARIO)
    worlds = tmp_path / "worlds"
    worlds.mkdir()
    for name in ("depot.yaml", "warehouse.yaml"):
        (worlds / name).write_text(_WORLD)
    vast = tmp_path / "campaign.vast"
    vast.write_text(textwrap.dedent(f"""\
        version: 2
        metadata: {{name: sim-channel}}
        configuration:
        {configuration}
        execution:
          mode: ros2
          containers:
            simulation:
              backend: roqsim
              config: worlds/depot.yaml
              image: sim:latest
            scenario: {{image: scen:latest}}
          runs: 1
          scenario_file: scenario.osc
        """))
    return vast


def _compose(vast, tmp_path):
    data = generate_scenario_variations(
        str(vast), progress_update_callback=lambda m: None,
        output_dir=str(tmp_path / "gen"), use_cache=False)
    return data


def test_a_world_sweep_gives_each_configuration_its_own_world(tmp_path):
    vast = _project(tmp_path, textwrap.indent(textwrap.dedent("""\
        - name: sweep
          variations:
          - ParameterVariationList:
              sim: config
              values: [worlds/depot.yaml, worlds/warehouse.yaml]
        """), "        ").lstrip())
    data = _compose(vast, tmp_path)

    worlds = [c["sim"]["config"] for c in data["configs"]]
    assert worlds == ["worlds/depot.yaml", "worlds/warehouse.yaml"]
    # Both travel. The campaign default is only a default; a world that is not staged is a
    # run that cannot start, and it fails after the image pull.
    assert {"worlds/depot.yaml", "worlds/warehouse.yaml"} <= set(data["_run_files"])


def test_an_override_sweep_keeps_one_world_and_varies_inside_it(tmp_path):
    vast = _project(tmp_path, textwrap.indent(textwrap.dedent("""\
        - name: friction
          variations:
          - ParameterVariationList:
              sim: plugins.floorplan.floor.friction
              values: [0.6, 1.4]
        """), "        ").lstrip())
    data = _compose(vast, tmp_path)

    assert [c["sim"]["overrides"]["plugins"]["floorplan"]["floor"]["friction"]
            for c in data["configs"]] == [0.6, 1.4]
    assert all(c["sim"]["config"] == "worlds/depot.yaml" for c in data["configs"])


def test_a_fixed_sim_block_on_a_configuration_is_merged(tmp_path):
    vast = _project(tmp_path, textwrap.indent(textwrap.dedent("""\
        - name: roofless
          sim:
            overrides: {plugins: {ceiling: {enabled: false}}}
          variations:
          - ParameterVariationList:
              scenario: goal_pose
              values: [[1.0], [2.0]]
        """), "        ").lstrip())
    data = _compose(vast, tmp_path)

    for config in data["configs"]:
        assert config["sim"]["overrides"]["plugins"]["ceiling"]["enabled"] is False
        assert config["sim"]["config"] == "worlds/depot.yaml"


def test_a_variation_wins_over_the_configurations_fixed_value(tmp_path):
    vast = _project(tmp_path, textwrap.indent(textwrap.dedent("""\
        - name: both
          sim:
            config: worlds/depot.yaml
          variations:
          - ParameterVariationList:
              sim: config
              values: [worlds/warehouse.yaml]
        """), "        ").lstrip())
    data = _compose(vast, tmp_path)
    assert data["configs"][0]["sim"]["config"] == "worlds/warehouse.yaml"


def test_every_configuration_records_what_its_simulator_was_given(tmp_path):
    """`sim.config` is a record beside `scenario.config`, written even when nothing varied.

    A reader should never have to work out whether a missing file means "no simulator" or
    "nothing varied" -- and its two values are the arguments that replay the cell by hand.
    """
    vast = _project(tmp_path, textwrap.indent(textwrap.dedent("""\
        - name: sweep
          variations:
          - ParameterVariationList:
              sim: config
              values: [worlds/depot.yaml, worlds/warehouse.yaml]
        """), "        ").lstrip())
    data = _compose(vast, tmp_path)
    out = tmp_path / "campaign"
    prepare_campaign_configs(str(out), data)

    for config in data["configs"]:
        recorded = yaml.safe_load(
            (out / config["name"] / "_config" / SIM_CONFIG_FILE).read_text())
        assert recorded["config"] == config["sim"]["config"]
        assert (out / config["name"] / "_config" / "scenario.config").is_file()


def test_the_world_reaches_the_container_that_runs_it(tmp_path):
    """What each job starts: the world on argv, the overrides as a mounted document."""
    from robovast.common.simulators import sim_job_overlay

    vast = _project(tmp_path, textwrap.indent(textwrap.dedent("""\
        - name: sweep
          variations:
          - ParameterVariationList:
              sim: config
              values: [worlds/depot.yaml, worlds/warehouse.yaml]
          - ParameterVariationList:
              sim: plugins.floorplan.floor.friction
              values: [0.6]
        """), "        ").lstrip())
    data = _compose(vast, tmp_path)
    execution = data["execution"]

    commands = []
    for config in data["configs"]:
        overlay = sim_job_overlay(execution, config["sim"], os.path.dirname(str(vast)))
        commands.append(overlay["command"])
        # The overrides are a document, not argv: they are structured, and a command line
        # loses that to quoting while a file keeps it in the results.
        assert overlay["document"] == {
            "plugins": {"floorplan": {"floor": {"friction": 0.6}}}}
        assert "--override" in overlay["command"]
        assert SIM_OVERRIDES_MOUNT in overlay["command"]

    # The worlds are mounted where the campaign's run_files are, and differ per cell --
    # which is the whole point. Everything else about the command is identical.
    assert commands[0] != commands[1]
    assert "/config/worlds/depot.yaml" in commands[0]
    assert "/config/worlds/warehouse.yaml" in commands[1]
    assert [c for c in commands[0] if "worlds/" not in c] == \
           [c for c in commands[1] if "worlds/" not in c]
