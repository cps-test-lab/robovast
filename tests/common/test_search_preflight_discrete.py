# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""The pre-flight says how many draws it made AND how many cells they covered.

A repeated draw is one cell: ``ParamSet.id`` is derived from the values and results are
addressed by it, so the batch composes it once. On a coarse space that gap is large -- a
random or TPE draw over four cells with ``per_batch: 8`` lands on three of them -- and a
preview reporting only the survivors reads as a strategy that proposed fewer sets than the
campaign asked for, which is not what happened.

Both counts are therefore reported, and the infeasible ratio is stated against the cells: a
space is not part-unrealizable for having been sampled twice in the same spot.
"""

import textwrap

from robovast.common.config_validation import validate_project_file
from robovast.search.compose import preview_search_sample

from .test_search_preflight import _INFEASIBLE_VARIATION, _SCENARIO

#: The same stand-in variation with its threshold moved out of reach, so nothing is
#: infeasible and the counts under test are the only thing moving.
_ALWAYS_FEASIBLE = _INFEASIBLE_VARIATION.replace(">= 0.5", ">= 99.0")


def _discrete_project(tmp_path, *, values, per_batch, variation):
    (tmp_path / "scenario.osc").write_text(_SCENARIO)
    (tmp_path / "myvar.py").write_text(variation)
    vast = tmp_path / "campaign.vast"
    vast.write_text(textwrap.dedent(f"""\
        version: 3
        metadata: {{name: search-preflight-discrete}}
        execution:
          containers:
            scenario: {{image: 'family:robovast'}}
          runs: 3
          scenario_file: scenario.osc
        search:
          strategy: random
          search_space:
            speed: {{type: choice, values: {values}}}
          variations:
          - myvar.py:SometimesInfeasible:
              speed: $speed
          extract:
            plugin: failure_rate
          objectives:
          - {{name: failure_rate, direction: maximize}}
          per_batch: {per_batch}
          budget:
          - batches: 2
          seed: 0
    """))
    return vast


def test_the_preview_reports_the_draws_and_the_cells_they_covered(tmp_path):
    vast = _discrete_project(tmp_path, values=[0.1, 0.2], per_batch=8,
                             variation=_ALWAYS_FEASIBLE)

    sample = preview_search_sample(str(vast))

    assert sample["sampled"] == 8            # what the strategy DREW
    assert sample["distinct"] == 2           # the cells those draws covered
    assert sample["composed"] == 2
    assert sample["infeasible"] == []
    assert len(sample["configs"]) == 2


def test_a_coarse_space_is_still_a_valid_campaign(tmp_path):
    """Proposing one cell twice is legal -- the campaign evaluates it once and tells the
    strategy once for it -- so the pre-flight must not report it as a problem."""
    vast = _discrete_project(tmp_path, values=[0.1, 0.2], per_batch=8,
                             variation=_ALWAYS_FEASIBLE)

    report = validate_project_file(str(vast))

    assert report["valid"] is True
    assert [p for p in report["problems"] if p["stage"] == "search-composition"] == []
    assert report["configs"] == 2


def test_the_infeasible_ratio_counts_cells_not_draws(tmp_path):
    """Both levels are unrealizable here, so the honest ratio is 2 of 2 -- the cells --
    and not 2 of 8, which would describe a space that is three-quarters fine."""
    vast = _discrete_project(tmp_path, values=[0.5, 0.6], per_batch=8,
                             variation=_INFEASIBLE_VARIATION)

    report = validate_project_file(str(vast))

    composition = [p for p in report["problems"] if p["stage"] == "search-composition"]
    problem, = composition
    assert "2 of 2 distinct" in problem["message"]
    assert "of 8" not in problem["message"]
