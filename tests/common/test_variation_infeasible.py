# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""``tolerate_infeasible`` in ``generate_scenario_variations``.

A ``VariationInfeasibleError`` (a specific parameter draw cannot be realized, as
opposed to a plugin bug) is only ever safe to drop and continue past when the
caller opts in (``tolerate_infeasible=True`` -- search composition). Every other
exception, and a plain ``tolerate_infeasible=False`` call, must still abort
composition entirely: a bug must never be silently absorbed as "just an
infeasible draw".
"""

import textwrap

import pytest

from robovast.common.config_generation import generate_scenario_variations
from robovast.common.variation.base_variation import VariationInfeasibleError

_SCENARIO = """\
import osc.robotics

scenario nav:
    goal_pose: pose_3d = pose_3d()
    do serial:
        wait elapsed(1s)
"""

_INFEASIBLE_VARIATION = textwrap.dedent("""\
    from robovast.common.variation.base_variation import Variation, VariationInfeasibleError

    class InfeasibleVariation(Variation):
        def variation(self, in_configs):
            raise VariationInfeasibleError("no valid placement for this draw")
""")

_BUGGY_VARIATION = textwrap.dedent("""\
    from robovast.common.variation.base_variation import Variation

    class BuggyVariation(Variation):
        def variation(self, in_configs):
            raise RuntimeError("boom - an actual bug, not an infeasible draw")
""")


def _project(tmp_path, configuration, variation_source):
    (tmp_path / "scenario.osc").write_text(_SCENARIO)
    (tmp_path / "myvar.py").write_text(variation_source)
    vast = tmp_path / "campaign.vast"
    vast.write_text(textwrap.dedent(f"""\
        version: 2
        metadata: {{name: infeasible-test}}
        configuration:
        {configuration}
        execution:
          containers:
            scenario: {{image: scen:latest}}
          runs: 1
          scenario_file: scenario.osc
        """))
    return vast


def _two_block_config(failing_class):
    return textwrap.indent(textwrap.dedent(f"""\
        - name: ok
        - name: bad
          variations:
          - myvar.py:{failing_class}: {{}}
        """), "        ").lstrip()


def test_infeasible_error_propagates_by_default(tmp_path):
    """``tolerate_infeasible`` defaults to False (batch mode's behavior): a
    VariationInfeasibleError must abort composition entirely, same as any other
    exception, unless a caller explicitly opts in."""
    vast = _project(tmp_path, _two_block_config("InfeasibleVariation"), _INFEASIBLE_VARIATION)
    with pytest.raises(VariationInfeasibleError, match="no valid placement"):
        generate_scenario_variations(str(vast), use_cache=False)


def test_infeasible_error_is_tolerated_when_opted_in(tmp_path):
    """With tolerate_infeasible=True (search composition's opt-in), only the
    affected config block is dropped; the rest of the campaign still composes."""
    vast = _project(tmp_path, _two_block_config("InfeasibleVariation"), _INFEASIBLE_VARIATION)
    data = generate_scenario_variations(str(vast), use_cache=False, tolerate_infeasible=True)
    names = [c["name"] for c in data["configs"]]
    assert names == ["ok"]


def test_a_real_bug_is_never_tolerated(tmp_path):
    """A plain RuntimeError (a plugin bug, not an infeasible draw) must always
    abort composition -- even when the caller opted into tolerating infeasible
    draws. Silently swallowing it would hide a real defect as if it were just a
    probabilistic infeasible parameter combination."""
    vast = _project(tmp_path, _two_block_config("BuggyVariation"), _BUGGY_VARIATION)
    with pytest.raises(RuntimeError, match="boom"):
        generate_scenario_variations(str(vast), use_cache=False, tolerate_infeasible=True)
