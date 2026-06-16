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
