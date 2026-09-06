# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""A plugin refusing the parameters it was handed, during a search.

A search proposes values; a variation plugin declares which values it accepts. Where the
two disagree the plugin's config model refuses the draw -- and until that refusal had a
name it was an anonymous ``ValueError`` raised where nothing could label it, so it read as
a defect, ended the campaign, and took every batch before it with it.

It is now :class:`VariationConfigError`: skipped by a search exactly like an unrealizable
draw, still fatal for a batch (whose every cell is stated, so a refused value there is the
sweep's own bounds being wrong), and raised where composition can name the plugin and the
config block.
"""

import textwrap

import pytest

from robovast.common.config_generation import generate_scenario_variations
from robovast.common.variation.base_variation import VariationConfigError

_SCENARIO = """\
import osc.robotics

scenario nav:
    speed: length = 1.0m
    do serial:
        wait elapsed(1s)
"""

#: A plugin that accepts one value and refuses the rest, in its config model -- the shape
#: every real plugin's domain check has (ObstacleVariationWithDistanceTrigger places a
#: single obstacle, so it refuses any amount but 1).
_PICKY_VARIATION = textwrap.dedent("""\
    from pydantic import model_validator

    from robovast.common.variation.base_variation import Variation, VariationConfig

    class PickyConfig(VariationConfig):
        limit: int

        @model_validator(mode='after')
        def _only_one(self):
            if self.limit != 1:
                raise ValueError(f"this plugin only supports limit=1, got {self.limit}")
            return self

    class PickyVariation(Variation):
        CONFIG_CLASS = PickyConfig

        def variation(self, in_configs):
            return in_configs
""")


def _project(tmp_path, *limits):
    (tmp_path / "scenario.osc").write_text(_SCENARIO)
    (tmp_path / "picky.py").write_text(_PICKY_VARIATION)
    blocks = "".join(textwrap.dedent(f"""\
        - name: cell{i}
          variations:
          - picky.py:PickyVariation: {{limit: {limit}}}
        """) for i, limit in enumerate(limits))
    vast = tmp_path / "campaign.vast"
    vast.write_text(textwrap.dedent("""\
        version: 3
        metadata: {name: refused-draw-test}
        configuration:
        """) + textwrap.indent(blocks, "  ") + textwrap.dedent("""\
        execution:
          containers:
            scenario: {image: scen:latest}
          runs: 1
          scenario_file: scenario.osc
        """))
    return vast


def test_a_refused_draw_is_dropped_when_infeasible_draws_are_tolerated(tmp_path):
    """What a search needs: the cell whose value the plugin will not take is dropped and
    the rest of the batch composes, instead of one proposal ending the campaign."""
    vast = _project(tmp_path, 1, 7)
    data = generate_scenario_variations(str(vast), use_cache=False, tolerate_infeasible=True)
    assert [c["_config_name"] for c in data["configs"]] == ["cell0"]


def test_a_batch_still_refuses_it(tmp_path):
    """A sweep states every cell, so a value a plugin will not accept is the sweep's own
    bounds being wrong -- and a stated level must never be skipped silently."""
    vast = _project(tmp_path, 1, 7)
    with pytest.raises(VariationConfigError, match="only supports limit=1"):
        generate_scenario_variations(str(vast), use_cache=False)


def test_the_refusal_names_the_plugin_and_the_config(tmp_path):
    """Raised while the plugin is being constructed, which used to happen outside the
    handler that knows either name: 'Config validation failed' was the whole of what a
    reader got for one value of one plugin among several."""
    vast = _project(tmp_path, 7)
    with pytest.raises(VariationConfigError) as excinfo:
        generate_scenario_variations(str(vast), use_cache=False)
    message = str(excinfo.value)
    assert "PickyVariation" in message
    assert "cell0" in message


def test_a_plugin_that_breaks_is_not_a_refused_draw(tmp_path):
    """The line the narrow type exists to hold: a config model can only report that these
    values are unacceptable, so tolerating it cannot swallow a plugin whose own logic
    fails. That still aborts, tolerated draws or not."""
    (tmp_path / "scenario.osc").write_text(_SCENARIO)
    (tmp_path / "boom.py").write_text(textwrap.dedent("""\
        from robovast.common.variation.base_variation import Variation

        class BoomVariation(Variation):
            def variation(self, in_configs):
                raise RuntimeError("boom - an actual bug")
    """))
    vast = tmp_path / "campaign.vast"
    vast.write_text(textwrap.dedent("""\
        version: 3
        metadata: {name: refused-draw-test}
        configuration:
        - name: cell0
          variations:
          - boom.py:BoomVariation: {}
        execution:
          containers:
            scenario: {image: scen:latest}
          runs: 1
          scenario_file: scenario.osc
        """))
    with pytest.raises(RuntimeError, match="boom"):
        generate_scenario_variations(str(vast), use_cache=False, tolerate_infeasible=True)
