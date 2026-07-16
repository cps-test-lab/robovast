# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Tests for editable, re-runnable per-campaign postprocessing (versioned override)."""

from pathlib import Path

import pytest
import yaml

from robovast.service.postprocessing_edit import (effective_vast,
                                                  get_postprocessing,
                                                  update_postprocessing)


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
