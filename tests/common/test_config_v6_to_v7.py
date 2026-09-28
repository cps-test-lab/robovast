# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Config 6 -> 7: the top-level ``general:`` section is gone, and so is its plugin argument."""

import inspect

import pytest

from robovast.common.common import load_config
from robovast.common.migrations import SUPPORTED_CONFIG_VERSION
from robovast.common.migrations.config.v6_to_v7 import migrate
from robovast.common.variation import Variation

_BODY = ("metadata: {name: x}\n"
         "general:\n  map_resolution: 0.05\n"
         "execution:\n  containers: {scenario: {image: img}}\n  runs: 1\n"
         "  scenario_file: s.osc\n")


def test_the_step_drops_general():
    out = migrate({"version": 6, "metadata": {"name": "x"}, "general": {"a": 1}})

    assert "general" not in out
    assert out == {"version": 7, "metadata": {"name": "x"}}


def test_a_current_file_declaring_general_is_refused_naming_the_key(tmp_path):
    vast = tmp_path / "campaign.vast"
    vast.write_text(f"version: {SUPPORTED_CONFIG_VERSION}\n" + _BODY, encoding="utf-8")

    with pytest.raises(ValueError, match=r"\bgeneral\b"):
        load_config(str(vast))


def test_an_older_file_is_read_without_general(tmp_path):
    vast = tmp_path / "campaign.vast"
    vast.write_text("version: 6\n" + _BODY, encoding="utf-8")

    config = load_config(str(vast), upgrade=True)

    assert config["version"] == SUPPORTED_CONFIG_VERSION
    assert "general" not in config


def test_a_variation_is_constructed_without_general_parameters(tmp_path):
    assert "general_parameters" not in inspect.signature(Variation.__init__).parameters

    variation = Variation(str(tmp_path), {"k": 1}, lambda *_: None, "s.osc", str(tmp_path))

    assert variation.parameters == {"k": 1}
    assert not hasattr(variation, "general_parameters")
