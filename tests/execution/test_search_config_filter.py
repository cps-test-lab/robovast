# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""A search campaign must refuse a ``config_filter`` rather than ignore it.

``config_filter`` + ``runs=1`` is the documented way to pilot one configuration
before committing to a sweep. A search names its configurations after parameter
sets the strategy has not drawn yet, so the filter cannot select anything -- and
the launch path used to accept it and silently drop it. The caller asking to run
*one* configuration got the entire search budget instead: the exact compute the
pilot exists to avoid, spent without a word.
"""

import pytest

from robovast.common.config import validate_config
from robovast.execution.backends import CampaignConfigError
from robovast.execution.controller import run_search_campaign

_VAST = {
    "version": 3,
    "metadata": {"name": "search-filter"},
    "execution": {
        "containers": {"scenario": {"image": "scen:latest"}},
        "runs": 1,
        "scenario_file": "scenario.osc",
    },
    "search": {
        "strategy": "random",
        "search_space": {"x": {"type": "float", "low": 0.0, "high": 1.0}},
        "extract": {"plugin": "failure_rate"},
        "objectives": [{"name": "failure_rate", "direction": "maximize"}],
        "per_batch": 2,
        "budget": [{"batches": 1}],
        "seed": 0,
    },
}


def test_a_config_filter_is_refused_not_ignored(tmp_path):
    campaign_config = validate_config(_VAST)
    with pytest.raises(CampaignConfigError) as excinfo:
        run_search_campaign(str(tmp_path / "campaign.vast"), campaign_config,
                            str(tmp_path), 1, config_filter="cfg-*")

    message = str(excinfo.value)
    assert "config_filter" in message
    assert "cfg-*" in message                    # names what was rejected
    assert "per_batch" in message                # ...and what to do instead
    # A user error, so the durable failure record carries no stack trace.
    assert excinfo.value.include_traceback is False


def test_no_filter_still_reaches_the_search_loop(tmp_path):
    """The guard must reject only the filter, not every search launch."""
    campaign_config = validate_config(_VAST)
    # Without a filter it proceeds past the guard and fails later, on the missing
    # project files -- which is how we know the guard let it through.
    with pytest.raises(Exception) as excinfo:
        run_search_campaign(str(tmp_path / "campaign.vast"), campaign_config,
                            str(tmp_path), 1)
    assert "config_filter" not in str(excinfo.value)
