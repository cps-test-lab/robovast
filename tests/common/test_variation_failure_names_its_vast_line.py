# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""A variation that fails names the ``.vast`` line it is written on.

The frames say where the plugin broke; the config block and the line say which entry of
the file ran it. Both a bug and a refusal carry it, because either way the reader's next
move starts in the ``.vast``.
"""

import textwrap

import pytest

from robovast.common.config_generation import generate_scenario_variations
from robovast.common.config_location import variation_line
from robovast.common.variation.base_variation import VariationConfigError, VariationFailed

_SCENARIO = """\
import osc.robotics

scenario nav:
    speed: length = 1.0m
    do serial:
        wait elapsed(1s)
"""

_PLUGINS = textwrap.dedent("""\
    from pydantic import model_validator

    from robovast.common.variation.base_variation import Variation, VariationConfig

    class Fine(Variation):
        def variation(self, in_configs):
            return in_configs

    class Broken(Variation):
        def variation(self, in_configs):
            raise KeyError("no such slot")

    class PickyConfig(VariationConfig):
        limit: int

        @model_validator(mode='after')
        def _only_one(self):
            if self.limit != 1:
                raise ValueError(f"this plugin only supports limit=1, got {self.limit}")
            return self

    class Picky(Variation):
        CONFIG_CLASS = PickyConfig

        def variation(self, in_configs):
            return in_configs
""")

#: Two blocks; the failing entry is the second variation of the second block, so the line
#: reported has to be that one and not the first match of anything.
_VAST = """\
version: 5
metadata: {name: vast-line-test}
configuration:
- name: cell0
  variations:
  - plugins.py:Fine: {}
- name: cell1
  variations:
  - plugins.py:Fine: {}
  - plugins.py:%s: %s
execution:
  containers:
    scenario: {image: 'family:robovast'}
  runs: 1
  scenario_file: scenario.osc
"""


def _project(tmp_path, failing, params="{}"):
    (tmp_path / "scenario.osc").write_text(_SCENARIO)
    (tmp_path / "plugins.py").write_text(_PLUGINS)
    vast = tmp_path / "campaign.vast"
    vast.write_text(_VAST % (failing, params))
    return vast


def test_a_bug_names_the_line_the_variation_is_written_on(tmp_path):
    vast = _project(tmp_path, "Broken")
    with pytest.raises(VariationFailed) as excinfo:
        generate_scenario_variations(str(vast), use_cache=False)
    message = str(excinfo.value)
    assert message.startswith("campaign.vast:10: config 'cell1': Variation failed. Broken:")
    assert "in variation" in message


def test_a_refusal_names_it_too(tmp_path):
    vast = _project(tmp_path, "Picky", "{limit: 7}")
    with pytest.raises(VariationConfigError) as excinfo:
        generate_scenario_variations(str(vast), use_cache=False)
    assert str(excinfo.value).startswith("campaign.vast:10: config 'cell1': ")
    assert excinfo.value.config_name == "cell1"


def test_the_line_is_the_blocks_own_entry(tmp_path):
    vast = _project(tmp_path, "Broken")
    assert variation_line(str(vast), "cell0", "plugins.py:Fine") == 6
    assert variation_line(str(vast), "cell1", "plugins.py:Fine") == 9
    assert variation_line(str(vast), "cell1", "plugins.py:Broken") == 10


def test_a_variation_a_block_takes_from_a_preset_is_found_where_it_is_written(tmp_path):
    """``use:`` copies a preset's variations into the block; the file writes them once."""
    vast = tmp_path / "campaign.vast"
    vast.write_text(textwrap.dedent("""\
        version: 5
        configuration_presets:
          contact:
            variations:
            - plugins.py:Broken: {}
        configuration:
        - name: cell0
          use: [contact]
        """))
    assert variation_line(str(vast), "cell0", "plugins.py:Broken") == 5


def test_a_variation_the_file_never_names_has_no_line(tmp_path):
    vast = _project(tmp_path, "Broken")
    assert variation_line(str(vast), "cell1", "plugins.py:Other") is None
    assert variation_line(str(tmp_path / "missing.vast"), "cell1", "plugins.py:Broken") is None
