# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A campaign configuration's contribution, derived from what the campaign froze.

Nothing is stored: the markers come from the configuration recorded in
``_transient/configurations.yaml`` and the variation types of the block it was composed from,
found in the campaign's own ``.vast``. These pin both composition shapes (a batch block and a
search template) and that every way of not finding the answer says so.
"""

import pytest
import yaml

from robovast.common.scene_markers import campaign_contribution

pytest.importorskip("robovast_nav")

_CONFIG = {
    "name": "blk-1",
    "config": {
        "map_file": "maps/room.yaml",
        "start_pose": {"position": {"x": 0.0, "y": 0.0}, "orientation": {"yaw": 0.0}},
        "goal_poses": [{"position": {"x": 4.0, "y": 0.0}, "orientation": {"yaw": 0.0}}],
    },
    "_path": [{"x": 0.0, "y": 0.0}, {"x": 4.0, "y": 0.0}],
    "_goal_parameter_name": "goal_poses",
    "_config_name": "blk",
}


def _campaign(root, vast: dict, configs=(_CONFIG,)):
    (root / "_config").mkdir(parents=True)
    (root / "_transient").mkdir()
    (root / "_config" / "campaign.vast").write_text(yaml.safe_dump(vast), encoding="utf-8")
    (root / "_transient" / "configurations.yaml").write_text(
        yaml.safe_dump({"configs": list(configs)}), encoding="utf-8")
    return root


_BATCH = {"configuration": [{"name": "blk", "variations": [{"PathVariationRandom": {}}]}]}


def test_a_batch_configuration_gets_its_blocks_markers_and_campaign_relative_files(tmp_path):
    contribution = campaign_contribution(_campaign(tmp_path / "camp", _BATCH), "blk-1")
    assert contribution["errors"] == []
    paths = [m for m in contribution["markers"] if m["kind"] == "path"]
    assert [p["points"] for p in paths] == [[[0.0, 0.0], [4.0, 0.0]]]
    assert {m["label"] for m in contribution["markers"]} >= {"planned path", "start"}
    # Under _config/, where the campaign keeps its workspace -- not the workspace path.
    assert contribution["files"] == {"map": "_config/maps/room.yaml"}


def test_a_search_configuration_is_composed_from_the_search_template(tmp_path):
    """A search campaign's block is synthesised per draw and named by a hash, so its
    variation types are the ``search.variations`` template, not a ``configuration:`` block."""
    vast = {"search": {"variations": [{"PathVariationRandom": {"path_length": "${length}"}}]}}
    config = {**_CONFIG, "name": "c0f3-1-1", "_config_name": "c0f3"}
    contribution = campaign_contribution(_campaign(tmp_path / "camp", vast, [config]),
                                         "c0f3-1-1")
    assert contribution["errors"] == []
    assert [m["kind"] for m in contribution["markers"]].count("path") == 1


def test_an_unknown_configuration_is_refused_with_the_ones_that_exist(tmp_path):
    with pytest.raises(KeyError, match="blk-1"):
        campaign_contribution(_campaign(tmp_path / "camp", _BATCH), "nope")


def test_a_block_the_vast_does_not_have_is_refused_by_name(tmp_path):
    vast = {"configuration": [{"name": "other", "variations": []}]}
    with pytest.raises(ValueError, match="'blk'"):
        campaign_contribution(_campaign(tmp_path / "camp", vast), "blk-1")


def test_a_campaign_without_resolved_configurations_is_unknown(tmp_path):
    (tmp_path / "camp" / "_config").mkdir(parents=True)
    with pytest.raises(KeyError, match="configurations.yaml"):
        campaign_contribution(tmp_path / "camp", "blk-1")


def test_an_unresolvable_variation_type_is_reported_not_an_empty_view(tmp_path):
    vast = {"configuration": [{"name": "blk", "variations": [{"NoSuchVariation": {}}]}]}
    contribution = campaign_contribution(_campaign(tmp_path / "camp", vast), "blk-1")
    assert contribution["markers"] == []
    assert contribution["errors"], "an empty view must say why it is empty"
