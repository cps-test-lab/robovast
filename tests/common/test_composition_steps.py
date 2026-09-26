# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""Composing a ``.vast`` counts its steps, one per variation of each configuration block.

The counter rides the progress line, the one channel that also crosses the isolated
worker's stdout, and ``parse_composition_step`` reads it back; a campaign's
``Status.variation`` and the web launcher's config-name list both show it.
"""

import pytest

from robovast.common.config_generation import (generate_scenario_variations,
                                               parse_composition_step)
from robovast.execution.controller import filter_configs_by_name
from robovast.common.errors import CampaignConfigError

_SCENARIO = """\
import osc.robotics

scenario nav:
    speed: length = 1.0m
    do serial:
        wait elapsed(1s)
"""

_PLUGINS = """\
from robovast.common.variation.base_variation import Variation

class Fine(Variation):
    def variation(self, in_configs):
        return in_configs

class Empty(Variation):
    def variation(self, in_configs):
        return []
"""

_VAST = """\
version: 6
metadata: {name: step-test}
configuration:
- name: cell0
  variations:
  - plugins.py:Fine: {}
- name: cell1
  variations:
  - plugins.py:%s: {}
  - plugins.py:Fine: {}
execution:
  containers:
    scenario: {image: 'family:robovast'}
  runs: 1
  scenario_file: scenario.osc
"""


def _steps(tmp_path, second_block_first):
    (tmp_path / "scenario.osc").write_text(_SCENARIO)
    (tmp_path / "plugins.py").write_text(_PLUGINS)
    vast = tmp_path / "campaign.vast"
    vast.write_text(_VAST % second_block_first)
    lines = []
    generate_scenario_variations(str(vast), progress_update_callback=lines.append,
                                 use_cache=False)
    return [s for s in map(parse_composition_step, lines) if s is not None]


def test_each_variation_of_each_block_is_a_step(tmp_path):
    assert _steps(tmp_path, "Fine") == [(0, 3), (1, 3), (2, 3), (3, 3)]


def test_a_block_that_stops_early_still_reaches_its_end(tmp_path):
    """The variations a stopped pipeline skips are not left owed: the counter ends at total."""
    assert _steps(tmp_path, "Empty") == [(0, 3), (1, 3), (3, 3)]


def test_other_lines_are_not_steps():
    assert parse_composition_step("Start generating configs.") is None


def test_a_filter_selects_what_any_of_its_globs_matches():
    configs = [{"name": n} for n in ("a-1", "a-2", "b-1", "c-1")]
    picked = filter_configs_by_name(configs, "a-2, b-*")
    assert [c["name"] for c in picked] == ["a-2", "b-1"]


def test_a_filter_matching_nothing_lists_the_names():
    with pytest.raises(CampaignConfigError, match="Available configs"):
        filter_configs_by_name([{"name": "a-1"}], "x*,y*")


def test_a_campaign_publishes_its_steps_as_its_status():
    from robovast.execution.control_server import ControllerState
    from robovast.execution.controller import _variation_progress
    state = ControllerState()
    _variation_progress(state)("Composed variation 2 of 5.")
    assert state.snapshot().variation.model_dump() == {"done": 2, "total": 5}
