# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``results_processing.resources``: what a campaign may say about its postprocessing."""

import pytest
from pydantic import ValidationError

from robovast.common.config import PostprocessResourcesConfig, validate_config


def test_the_block_reaches_the_model_from_a_whole_config():
    """Declared under ``results_processing``, not under ``execution`` -- so a campaign
    sizes the step that converts its bags without touching what its runs were given."""
    config = validate_config({"version": 3,
                              "execution": {"containers": {"scenario": {"image": "ghcr.io/x/y:1"}},
                                            "runs": 1},
                              "results_processing": {"resources": {"cpu": 8,
                                                                   "memory": "16Gi"}}})
    assert config.results_processing.resources.cpu == 8
    assert config.results_processing.resources.memory == "16Gi"


def test_postprocessing_refuses_a_split_between_request_and_limit():
    """``cpu_limit`` is correct one section up and wrong here, so the refusal says why.

    ``extra='forbid'`` already rejects the key, but its own message ("extra inputs are not
    permitted") reads as a misspelling -- and someone writing it here has copied a block
    that is deliberate for the execution containers, where splitting the two buys density
    on work that is not under test. This pod is different for a reason worth stating: it
    shares nodes with trials.
    """
    for key in ("cpu_limit", "memory_limit"):
        with pytest.raises(ValidationError, match="the reservation is the ceiling"):
            PostprocessResourcesConfig(**{"cpu": 4, key: 8})


def test_postprocessing_resources_take_the_same_quantity_spellings():
    """Millicores and per-cluster lists, as the execution block does -- one vocabulary for
    resources rather than a second one that happens to live under results_processing."""
    assert PostprocessResourcesConfig(cpu="500m").cpu == "500m"
    assert PostprocessResourcesConfig(cpu=[{"ctx-a": 4}]).cpu == [{"ctx-a": 4}]
    with pytest.raises(ValidationError, match="not a CPU quantity"):
        PostprocessResourcesConfig(cpu="4Gi")
    with pytest.raises(ValidationError, match="not a memory quantity"):
        PostprocessResourcesConfig(memory="four")
