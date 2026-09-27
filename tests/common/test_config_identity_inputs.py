# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A configuration's identity covers every file its composition read.

The ``.vast`` names a map YAML; only the variation knows the image that YAML points at, so
the identity takes it from the files the variation reported reading.
"""

import pytest
import yaml

from robovast.common.config_generation import generate_scenario_variations
from robovast.common.config_identifier import compute_config_identifier
from robovast.common.execution import prepare_campaign_configs

from .test_path_variation_cache import _project
from .test_path_variation_cache_map_image import _redraw_image

_HIT = "Loaded configurations from cache"


def _identity(vast, tmp_path, name):
    """Compose *vast*, stage it, and return ``(config.yaml, served from cache)``."""
    messages = []
    data = generate_scenario_variations(str(vast), progress_update_callback=messages.append,
                                        output_dir=str(tmp_path / f"gen-{name}"))
    out = tmp_path / f"campaign-{name}"
    prepare_campaign_configs(str(out), data)
    (config,) = data["configs"]
    record = yaml.safe_load((out / config["name"] / "_config" / "config.yaml").read_text())
    return record, any(_HIT in m for m in messages)


def test_a_redrawn_map_image_changes_the_config_identifier(tmp_path):
    vast = _project(tmp_path)
    before, _ = _identity(vast, tmp_path, "a")
    assert "read_files" in before["sub_identifier"]
    _redraw_image(tmp_path)
    after, _ = _identity(vast, tmp_path, "b")
    assert after["config_identifier"] != before["config_identifier"]


def test_a_cached_composition_keeps_the_config_identifier(tmp_path):
    """A hit carries the block and read files a fresh composition hashes."""
    vast = _project(tmp_path)
    fresh, fresh_hit = _identity(vast, tmp_path, "a")
    cached, cached_hit = _identity(vast, tmp_path, "b")
    assert (fresh_hit, cached_hit) == (False, True)
    assert cached == fresh


def test_a_variation_is_identified_by_its_reference(tmp_path):
    """Not by its installed source, so the identity is the same on every host and release."""
    def identity(variations):
        return compute_config_identifier(str(tmp_path), {"name": "c"}, "", "", variations)[0]

    assert identity(["PathVariationRandom"]) == identity(["PathVariationRandom"])
    assert identity(["PathVariationRandom"]) != identity(["ObstacleVariation"])
    assert identity(["NotInstalledHere"])


def test_a_configuration_without_its_block_is_refused(tmp_path):
    """Staging hashes the block; one that did not come from composition has none to hash."""
    (tmp_path / "s.vast").write_text("version: 6\n", encoding="utf-8")
    (tmp_path / "s.osc").write_text("scenario x:\n    do serial:\n        wait elapsed(1s)\n",
                                    encoding="utf-8")
    with pytest.raises(KeyError, match="_config_block"):
        prepare_campaign_configs(str(tmp_path / "out"), {
            "vast": str(tmp_path / "s.vast"), "scenario_file": str(tmp_path / "s.osc"),
            "configs": [{"name": "c1", "config": {}}], "execution": {"runs": 1}})
