# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Integration: Compose drives the real generation chain (no Docker).

Uses the non-ROS ``quadrotor_landing`` example scenario to verify that each
sampled ParamSet collapses to exactly one generated config, synthesized from the
search-space overrides (search is self-contained; there is no ``configuration:``
template — that is mutually exclusive with ``search:``).
"""

import os
import textwrap

import pytest

from robovast.search.compose import Compose, config_name_for
from robovast.search.types import ParamSet

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EXAMPLE = os.path.join(REPO, "configs", "examples", "quadrotor_landing")

pytestmark = pytest.mark.skipif(
    not os.path.exists(os.path.join(EXAMPLE, "scenario.osc")),
    reason="quadrotor_landing example scenario not available",
)

# A search-style base: no configuration: block — Compose synthesizes each config
# purely from the param-set overrides.
BASE_VAST = textwrap.dedent("""\
    version: 1
    execution:
      image: ghcr.io/cps-test-lab/robovast:latest
      runs: 1
      scenario_file: scenario.osc
    """)


@pytest.fixture()
def base_vast():
    # Must live in the example dir so the relative scenario_file resolves.
    path = os.path.join(EXAMPLE, ".robovast_test_search_base.vast")
    with open(path, "w") as f:
        f.write(BASE_VAST)
    yield path
    os.remove(path)


def test_compose_yields_one_config_per_param_set(base_vast, tmp_path):
    compose = Compose(base_vast)
    param_sets = [
        ParamSet(values={"thrust_gain": 2.0, "mass": 1.5}),
        ParamSet(values={"thrust_gain": 0.5, "mass": 2.5}),
    ]
    campaign_data, name_by_id = compose.compose(param_sets, str(tmp_path / "art"))

    configs = campaign_data["configs"]
    assert len(configs) == 2
    names = {c["name"] for c in configs}
    assert names == {config_name_for(ps) for ps in param_sets}

    by_name = {c["name"]: c["config"] for c in configs}
    assert by_name[config_name_for(param_sets[0])]["thrust_gain"] == 2.0
    assert by_name[config_name_for(param_sets[0])]["mass"] == 1.5
    assert by_name[config_name_for(param_sets[1])]["thrust_gain"] == 0.5

    # The temp .vast is deleted; "vast" must point at a file that still exists
    # (downstream prepare_campaign_configs reads/copies it).
    assert os.path.exists(campaign_data["vast"])
    assert campaign_data["vast"] == os.path.abspath(base_vast)


# A search base whose search: block carries a variations template that references
# a searched variable ($tg) — exercises substitution through the real generation
# chain via the dependency-free ParameterVariationList plugin.
TEMPLATE_VAST = textwrap.dedent("""\
    version: 1
    execution:
      image: ghcr.io/cps-test-lab/robovast:latest
      runs: 1
      scenario_file: scenario.osc
    search:
      strategy: random
      search_space:
        tg: {type: float, low: 0.3, high: 3.0}
      variations:
      - ParameterVariationList:
          name: thrust_gain
          values: ["$tg"]
      extract: {plugin: failure_rate}
      objectives: [{name: failure_rate}]
      per_batch: 2
    """)


@pytest.fixture()
def template_vast():
    path = os.path.join(EXAMPLE, ".robovast_test_search_tmpl.vast")
    with open(path, "w") as f:
        f.write(TEMPLATE_VAST)
    yield path
    os.remove(path)


def test_compose_substitutes_variation_template(template_vast, tmp_path):
    compose = Compose(template_vast)
    param_sets = [ParamSet(values={"tg": 2.0}), ParamSet(values={"tg": 0.8})]
    campaign_data, name_by_id = compose.compose(param_sets, str(tmp_path / "art"))

    configs = campaign_data["configs"]
    assert len(configs) == 2  # one config per param set (no expansion)
    # name_by_id maps each ParamSet to its *produced* config name (the variation
    # renames its output, e.g. c<id>-1), which is the dir the evaluator reads.
    by_name = {c["name"]: c["config"] for c in configs}
    assert by_name[name_by_id[param_sets[0].id]]["thrust_gain"] == 2.0
    assert by_name[name_by_id[param_sets[1].id]]["thrust_gain"] == 0.8


def test_compose_quadrotor_wind_variation(tmp_path):
    # The shipped quadrotor search vast uses a LOCAL variation plugin
    # (variations/wind.py:WindFieldVariation) to derive wind_strength from
    # wind_speed + turbulence, while thrust_gain/mass/descent_rate fall back to
    # direct scenario params. Exercises both routes through the real chain.
    vast = os.path.join(EXAMPLE, "quadrotor_landing_search.vast")
    if not os.path.exists(vast):
        pytest.skip("quadrotor_landing_search.vast not available")
    compose = Compose(vast)
    ps = ParamSet(values={"thrust_gain": 1.5, "mass": 1.2, "descent_rate": 1.0,
                          "wind_speed": 10.0, "turbulence": 0.2})
    campaign_data, name_by_id = compose.compose([ps], str(tmp_path / "art"))

    assert len(campaign_data["configs"]) == 1
    cfg = next(c for c in campaign_data["configs"]
               if c["name"] == name_by_id[ps.id])["config"]
    # Variation-computed: 0.035 * 10**2 * (1 + 0.2) = 4.2
    assert cfg["wind_strength"] == pytest.approx(4.2)
    # Fallback direct scenario params survive unchanged.
    assert cfg["thrust_gain"] == 1.5 and cfg["mass"] == 1.2


def test_compose_rejects_expanding_variation(template_vast, tmp_path):
    # A two-element values list makes ParameterVariationList expand a param set
    # into two configs, violating the 1:1 contract — Compose must reject it.
    import yaml
    cfg = yaml.safe_load(TEMPLATE_VAST)
    cfg["search"]["variations"][0]["ParameterVariationList"]["values"] = ["$tg", 0.5]
    bad = os.path.join(EXAMPLE, ".robovast_test_search_bad.vast")
    with open(bad, "w") as f:
        yaml.safe_dump(cfg, f)
    try:
        compose = Compose(bad)
        with pytest.raises(ValueError, match="expanded param set"):
            compose.compose([ParamSet(values={"tg": 2.0})], str(tmp_path / "art"))
    finally:
        os.remove(bad)
