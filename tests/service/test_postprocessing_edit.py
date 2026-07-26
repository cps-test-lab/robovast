# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Editing postprocessing/visualization overwrites the campaign's own ``_config`` .vast.

Postprocessing config is not captured data (the raw rosbags are the ground truth), so an
edit overwrites the ``results_processing.postprocessing`` / ``visualization`` block of
``_config/<name>.vast`` in place — no override files, no revisions. Other blocks are
preserved.
"""

import pytest
import yaml

from robovast.service.postprocessing_edit import (campaign_vast,
                                                  get_postprocessing,
                                                  get_postprocessing_source,
                                                  get_visualization,
                                                  update_postprocessing,
                                                  update_postprocessing_source,
                                                  update_visualization)


@pytest.fixture
def campaign(tmp_path):
    cdir = tmp_path / "camp-2026-07-16-120000"
    cfg = cdir / "_config"
    cfg.mkdir(parents=True)
    (cfg / "demo.vast").write_text(yaml.safe_dump({
        "version": 1,
        "configuration": [{"name": "sweep"}],          # an "as-ran" block to preserve
        "results_processing": {"postprocessing": ["rosbags_to_csv"]},
    }))
    return cdir


def test_get_reads_the_config_vast(campaign):
    info = get_postprocessing(campaign)
    assert info["entries"] == ["rosbags_to_csv"]
    assert "source" not in info and "revisions" not in info


def test_update_overwrites_config_in_place_no_override_dir(campaign):
    update_postprocessing(campaign, ["rosbags_to_csv", "compress"])
    # No override file/dir is created.
    assert not (campaign / "_control").exists()
    # The campaign's own .vast now carries the new block...
    data = yaml.safe_load(campaign_vast(campaign).read_text())
    assert data["results_processing"]["postprocessing"] == ["rosbags_to_csv", "compress"]
    # ...and the as-ran block is preserved.
    assert data["configuration"] == [{"name": "sweep"}]
    assert get_postprocessing(campaign)["entries"] == ["rosbags_to_csv", "compress"]


def test_update_is_idempotent_overwrite(campaign):
    update_postprocessing(campaign, ["rosbags_to_csv"])
    update_postprocessing(campaign, ["compress"])            # overwrite, not a new rev
    assert get_postprocessing(campaign)["entries"] == ["compress"]
    assert not (campaign / "_control").exists()


def test_invalid_entries_rejected(campaign):
    with pytest.raises(ValueError, match="must be a list"):
        update_postprocessing(campaign, "not-a-list")


def test_unknown_plugin_rejected(campaign):
    before = campaign_vast(campaign).read_text()
    with pytest.raises(ValueError, match="Unknown postprocessing plugin"):
        update_postprocessing(campaign, ["no-such-plugin-xyz"])
    # A rejected edit must not have touched the file.
    assert campaign_vast(campaign).read_text() == before


# -- YAML-text variants driving the webui rerun dialog -----------------------


def test_get_source_serializes_the_block(campaign):
    parsed = yaml.safe_load(get_postprocessing_source(campaign)["content"])
    assert parsed == {"results_processing": {"postprocessing": ["rosbags_to_csv"]}}


def test_update_source_overwrites_and_round_trips(campaign):
    content = yaml.safe_dump(
        {"results_processing": {"postprocessing": ["rosbags_to_csv", "compress"]}})
    update_postprocessing_source(campaign, content)
    assert get_postprocessing(campaign)["entries"] == ["rosbags_to_csv", "compress"]
    assert not (campaign / "_control").exists()


def test_update_source_preserves_other_results_processing_keys(campaign):
    cfg = campaign_vast(campaign)
    data = yaml.safe_load(cfg.read_text())
    data["results_processing"]["evaluation"] = {"metric": "x"}
    cfg.write_text(yaml.safe_dump(data))
    update_postprocessing_source(
        campaign, yaml.safe_dump({"results_processing": {"postprocessing": ["compress"]}}))
    out = yaml.safe_load(campaign_vast(campaign).read_text())
    assert out["results_processing"]["evaluation"] == {"metric": "x"}
    assert out["results_processing"]["postprocessing"] == ["compress"]


def test_update_source_missing_key_rejected(campaign):
    with pytest.raises(ValueError, match="results_processing"):
        update_postprocessing_source(campaign, yaml.safe_dump({"visualization": {}}))


def test_update_source_invalid_yaml_rejected(campaign):
    with pytest.raises(ValueError, match="invalid YAML"):
        update_postprocessing_source(campaign, "results_processing: [oops: :")


def test_visualization_edit_overwrites_config_in_place(campaign):
    update_visualization(
        campaign, yaml.safe_dump({"visualization": {"panels": ["playback"]}}))
    data = yaml.safe_load(campaign_vast(campaign).read_text())
    assert data["visualization"] == {"panels": ["playback"]}
    # postprocessing block and as-ran block untouched.
    assert data["results_processing"]["postprocessing"] == ["rosbags_to_csv"]
    assert data["configuration"] == [{"name": "sweep"}]
    assert yaml.safe_load(get_visualization(campaign)["content"]) == \
        {"visualization": {"panels": ["playback"]}}
