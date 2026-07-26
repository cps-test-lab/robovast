# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Tests for editable, re-runnable per-campaign postprocessing (versioned override)."""

from pathlib import Path

import pytest
import yaml

from robovast.service.postprocessing_edit import (effective_vast,
                                                  get_postprocessing,
                                                  get_postprocessing_source,
                                                  update_postprocessing,
                                                  update_postprocessing_source)


@pytest.fixture
def campaign(tmp_path):
    cdir = tmp_path / "camp-2026-07-16-120000"
    cfg = cdir / "_config"
    cfg.mkdir(parents=True)
    (cfg / "demo.vast").write_text(yaml.safe_dump({
        "version": 1,
        "results_processing": {"postprocessing": ["rosbags_to_csv"]},
    }))
    return cdir


def test_get_reads_config_snapshot_when_no_override(campaign):
    info = get_postprocessing(campaign)
    assert info["source"] == "demo.vast"
    assert info["entries"] == ["rosbags_to_csv"]
    assert info["revisions"] == []


def test_update_writes_versioned_override_without_touching_config(campaign):
    snapshot_before = (campaign / "_config" / "demo.vast").read_text()
    res = update_postprocessing(campaign, ["rosbags_to_csv", "compress"])
    assert res["revision"] == 1
    assert (campaign / "_control" / "postprocess" / "rev-1.vast").is_file()
    # the immutable snapshot is unchanged
    assert (campaign / "_config" / "demo.vast").read_text() == snapshot_before
    # effective config is now the override
    info = get_postprocessing(campaign)
    assert info["source"] == "rev-1.vast"
    assert info["entries"] == ["rosbags_to_csv", "compress"]
    assert info["revisions"] == [1]


def test_updates_are_monotonic_revisions(campaign):
    update_postprocessing(campaign, ["rosbags_to_csv"])
    update_postprocessing(campaign, ["compress"])
    r3 = update_postprocessing(campaign, ["rosbags_to_csv", "compress"])
    assert r3["revision"] == 3
    assert get_postprocessing(campaign)["revisions"] == [1, 2, 3]
    assert effective_vast(campaign).name == "rev-3.vast"


def test_invalid_entries_rejected(campaign):
    with pytest.raises(ValueError, match="must be a list"):
        update_postprocessing(campaign, "not-a-list")


def test_unknown_plugin_rejected(campaign):
    with pytest.raises(ValueError, match="Unknown postprocessing plugin"):
        update_postprocessing(campaign, ["no-such-plugin-xyz"])


# -- YAML-text variants driving the webui rerun dialog -----------------------


def test_get_source_serializes_effective_block(campaign):
    src = get_postprocessing_source(campaign)
    assert src["source"] == "demo.vast"
    parsed = yaml.safe_load(src["content"])
    assert parsed == {"results_processing": {"postprocessing": ["rosbags_to_csv"]}}


def test_update_source_writes_override_and_round_trips(campaign):
    snapshot_before = (campaign / "_config" / "demo.vast").read_text()
    content = yaml.safe_dump(
        {"results_processing": {"postprocessing": ["rosbags_to_csv", "compress"]}})
    res = update_postprocessing_source(campaign, content)
    assert res["revision"] == 1
    # the immutable snapshot is unchanged
    assert (campaign / "_config" / "demo.vast").read_text() == snapshot_before
    info = get_postprocessing(campaign)
    assert info["source"] == "rev-1.vast"
    assert info["entries"] == ["rosbags_to_csv", "compress"]


def test_update_source_preserves_other_results_processing_keys(campaign):
    # A sibling key under results_processing must survive an edit of postprocessing.
    cfg = campaign / "_config" / "demo.vast"
    data = yaml.safe_load(cfg.read_text())
    data["results_processing"]["evaluation"] = {"metric": "x"}
    cfg.write_text(yaml.safe_dump(data))
    content = yaml.safe_dump(
        {"results_processing": {"postprocessing": ["compress"]}})
    update_postprocessing_source(campaign, content)
    effective = yaml.safe_load(effective_vast(campaign).read_text())
    assert effective["results_processing"]["evaluation"] == {"metric": "x"}
    assert effective["results_processing"]["postprocessing"] == ["compress"]


def test_update_source_missing_key_rejected(campaign):
    with pytest.raises(ValueError, match="results_processing"):
        update_postprocessing_source(campaign, yaml.safe_dump({"visualization": {}}))


def test_update_source_invalid_yaml_rejected(campaign):
    with pytest.raises(ValueError, match="invalid YAML"):
        update_postprocessing_source(campaign, "results_processing: [oops: :")
