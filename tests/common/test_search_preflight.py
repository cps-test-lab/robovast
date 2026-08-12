# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""Pre-flight checks must actually compose a ``search:``-mode ``.vast``.

A search ``.vast`` has no top-level ``configuration:`` block -- its variations live
under ``search.variations`` and are only expanded per sampled ParamSet at run time.
Checking one with the batch path reported ``configs: 0, valid: true``: an empty file
and a 2000-run campaign were indistinguishable, and every infeasible draw a
pre-flight exists to catch went unseen until the campaign was already running.
"""

import textwrap

from robovast.common.config_validation import validate_project_file
from robovast.search.compose import preview_search_sample

_SCENARIO = """\
import osc.robotics

scenario nav:
    speed: string = '1.0'
    do serial:
        wait elapsed(1s)
"""

_INFEASIBLE_VARIATION = textwrap.dedent("""\
    from robovast.common.variation.base_variation import Variation, VariationInfeasibleError

    class SometimesInfeasible(Variation):
        \"\"\"Infeasible for draws at or above a threshold -- a stand-in for e.g.
        ObstacleVariation running out of placement budget on a short path.\"\"\"

        def variation(self, in_configs):
            out = []
            for c in in_configs:
                if float(self.parameters['speed']) >= 0.5:
                    raise VariationInfeasibleError(
                        f"cannot realize speed={self.parameters['speed']}")
                out.append(self.update_config(c, {'speed': str(self.parameters['speed'])}))
            return out
""")


def _search_project(tmp_path, *, low, high, per_batch=4):
    (tmp_path / "scenario.osc").write_text(_SCENARIO)
    (tmp_path / "myvar.py").write_text(_INFEASIBLE_VARIATION)
    vast = tmp_path / "campaign.vast"
    vast.write_text(textwrap.dedent(f"""\
        version: 2
        metadata: {{name: search-preflight}}
        execution:
          containers:
            scenario: {{image: scen:latest}}
          runs: 3
          scenario_file: scenario.osc
        search:
          strategy: random
          search_space:
            speed: {{type: float, low: {low}, high: {high}}}
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


def test_all_draws_feasible_reports_real_counts(tmp_path):
    """The counts describe one composed batch -- not the zero a batch-mode
    expansion of a search file reports."""
    vast = _search_project(tmp_path, low=0.0, high=0.4, per_batch=4)
    report = validate_project_file(str(vast))
    assert report["valid"] is True
    assert report["problems"] == []
    assert report["configs"] == 4
    assert report["runs_per_config"] == 3
    assert report["total_trials"] == 12


def test_infeasible_draws_are_reported_with_their_params(tmp_path):
    """Infeasible draws are advisories, not errors: the campaign skips them and
    keeps going, but the check must say how many and which -- that is the signal
    the search space is partly unrealizable."""
    vast = _search_project(tmp_path, low=0.5, high=1.0, per_batch=4)
    report = validate_project_file(str(vast))

    # Every draw is infeasible here, but the file itself is well-formed.
    assert report["valid"] is True
    assert report["configs"] == 0
    assert len(report["problems"]) == 1
    problem = report["problems"][0]
    assert problem["stage"] == "search-composition"
    assert "4 of 4" in problem["message"]
    assert "speed" in problem["message"]        # the offending params are named
    assert problem["field"] == "search.search_space"


def test_preview_sample_separates_composed_from_infeasible(tmp_path):
    """The shared helper both pre-flight paths use reports each half explicitly."""
    vast = _search_project(tmp_path, low=0.0, high=0.4, per_batch=3)
    sample = preview_search_sample(str(vast))
    assert sample["sampled"] == 3
    assert sample["composed"] == 3
    assert sample["infeasible"] == []
    assert len(sample["configs"]) == 3
    assert sample["runs_per_config"] == 3
