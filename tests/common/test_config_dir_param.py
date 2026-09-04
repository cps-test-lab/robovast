# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""A cell's own directory, handed to the trial that asks for it.

The scenario file is campaign-wide, so every path it writes relative to itself reaches the
campaign's copy. These pin the one route to a configuration's own copies that does not make
each campaign carry a parameter naming a path it already implies.
"""

import textwrap

import pytest

from robovast.common.config import CONFIG_DIR_PARAM
from robovast.common.config_generation import generate_scenario_variations

_SCENARIO = """\
import osc.robotics

scenario nav:
    goal: string = ''
{extra}
    do serial:
        wait elapsed(1s)
"""


def _project(tmp_path, configuration, declares_config_dir):
    extra = f"    {CONFIG_DIR_PARAM}: string\n" if declares_config_dir else ""
    (tmp_path / "scenario.osc").write_text(_SCENARIO.format(extra=extra))
    block = textwrap.indent(textwrap.dedent(configuration).strip("\n"), "  ")
    vast = tmp_path / "campaign.vast"
    vast.write_text(
        "version: 3\n"
        "metadata: {name: config-dir}\n"
        "configuration:\n"
        f"{block}\n"
        "execution:\n"
        "  containers:\n"
        "    scenario: {image: scen:latest}\n"
        "  runs: 1\n"
        "  scenario_file: scenario.osc\n")
    return str(vast)


def _compose(tmp_path, configuration, declares_config_dir=True):
    return generate_scenario_variations(
        variation_file=_project(tmp_path, configuration, declares_config_dir),
        output_dir=str(tmp_path / "out"), use_cache=False, isolate_plugins=False)


def test_a_scenario_that_declares_it_is_given_its_own_directory(tmp_path):
    data = _compose(tmp_path, """\
        - name: cell
          variations:
          - ParameterVariationList:
              scenario: goal
              values: ['a', 'b']
    """)
    got = {c["name"]: c["config"][CONFIG_DIR_PARAM] for c in data["configs"]}
    assert got == {"cell-1": "/config/cell-1", "cell-2": "/config/cell-2"}


def test_the_value_is_the_directory_the_cell_s_own_files_are_staged_in(tmp_path):
    """The point of it: `<config_dir>/<deploy path>` is this cell's copy, and the mount root
    is the campaign's -- which is what a path relative to the scenario file would have hit."""
    data = _compose(tmp_path, "- name: only\n")
    config = data["configs"][0]
    assert config["config"][CONFIG_DIR_PARAM] == "/config/" + config["name"]


def test_a_scenario_that_does_not_declare_it_is_given_nothing(tmp_path):
    """An override for an undeclared parameter is refused by scenario-execution, so a
    campaign whose trial never asks must not be handed one."""
    data = _compose(tmp_path, "- name: only\n", declares_config_dir=False)
    assert CONFIG_DIR_PARAM not in data["configs"][0]["config"]


def test_a_campaign_may_not_assign_it(tmp_path):
    """It says which cell is running, so a campaign setting it could point one cell's trial
    at another's configuration -- and the two would disagree about what ran."""
    with pytest.raises(ValueError, match="may not assign it"):
        _compose(tmp_path, f"""\
        - name: only
          parameters:
          - {CONFIG_DIR_PARAM}: /config/somewhere-else
    """)
