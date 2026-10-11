# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""What a notebook calls: a campaign, a node inside it, an archive or several campaigns in,
DataFrames out."""

import tarfile

import pandas as pd
import pytest

from robovast_data import Campaign, Corpus
from robovast_data import data as data_module
from robovast_data import open_data, read_runs, read_table, scope_of

from .conftest import nav_campaign


def test_runs_has_a_row_per_run_with_its_params(campaign):
    runs = Campaign(str(campaign), workers=1).runs
    assert list(zip(runs.config_name, runs.run_id)) == [("cfg-a", 0), ("cfg-a", 1),
                                                         ("cfg-b", 0)]
    assert list(runs.param_speed) == [0.5, 0.5, 1.0]


def test_a_table_with_params_carries_the_factor_columns(campaign):
    poses = Campaign(str(campaign), workers=1).table("poses", config="cfg-b", run=0,
                                                     with_params=True)
    assert set(poses.param_speed) == {1.0}
    assert set(poses.config_name) == {"cfg-b"}


@pytest.mark.parametrize("relative,expected", [
    ("", (None, None)),
    ("cfg-a", ("cfg-a", None)),
    ("cfg-a/1", ("cfg-a", 1)),
    ("cfg-a/1/rosbag2", ("cfg-a", 1)),
    ("_transient", (None, None)),
])
def test_a_path_selects_its_node(campaign, relative, expected):
    scope = scope_of(str(campaign / relative) if relative else str(campaign))
    assert (scope.config_name, scope.run_id) == expected
    assert scope.campaign_dir == str(campaign)


def test_open_data_answers_for_the_node_only(campaign):
    node = open_data(str(campaign / "cfg-a" / "1" / "rosbag2"), workers=1)
    assert list(node.runs.run_id) == [1]
    assert set(node.table("poses").run_id) == {1}
    assert node.sql("SELECT count(DISTINCT run_id) AS n FROM poses").n.iloc[0] == 1
    whole = Campaign(str(campaign / "cfg-a" / "1"), workers=1)
    assert len(whole.runs) == 3


def test_an_archive_answers_as_its_directory_does(campaign, tmp_path):
    archive = tmp_path / "download" / f"{campaign.name}.tar.gz"
    archive.parent.mkdir()
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(campaign, arcname=campaign.name)
    from_archive = Campaign(str(archive), workers=1)
    pd.testing.assert_frame_equal(from_archive.runs.drop(columns="campaign_id"),
                                  Campaign(str(campaign), workers=1).runs.drop(
                                      columns="campaign_id"))
    assert len(from_archive.table("poses", config="cfg-a", run=0)) == 244
    assert Campaign(str(archive), workers=1).scopes == from_archive.scopes, "extracted once"


def test_a_corpus_is_several_campaigns_with_their_ids(tmp_path):
    nav_campaign(tmp_path / "nav-1")
    nav_campaign(tmp_path / "nav-2")
    corpus = Corpus(str(tmp_path / "nav-*"), workers=1)
    assert sorted(corpus.table("poses").campaign_id.unique()) == ["nav-1", "nav-2"]
    assert len(read_table(str(tmp_path / "nav-*"), "poses")) == 2 * 244


def test_the_one_liners_equal_the_methods(campaign):
    pd.testing.assert_frame_equal(read_runs(str(campaign)), Campaign(str(campaign)).runs)
    pd.testing.assert_frame_equal(read_table(str(campaign), "poses", config="cfg-a", run=0),
                                  Campaign(str(campaign)).table("poses", config="cfg-a", run=0))


def test_a_large_table_says_so_before_it_is_read(campaign, monkeypatch):
    monkeypatch.setattr(data_module, "LARGE_TABLE_ROWS", 100)
    with pytest.warns(UserWarning, match="rows here"):
        Campaign(str(campaign), workers=1).table("poses")


def test_what_could_not_be_built_is_said(campaign):
    (campaign / "_execution").mkdir(exist_ok=True)
    (campaign / "_execution" / "tables.yaml").write_text(
        "groups:\n- bag_dir: rosbag2\n  plugins:\n  - {type: tf_to_csv, frames: all, "
        "require: [nowhere]}\n")
    with pytest.warns(UserWarning, match="nowhere"):
        poses = Campaign(str(campaign), workers=1).table("poses", config="cfg-a", run=0)
    assert poses.empty


def test_a_configurations_files_are_readable(campaign):
    files = campaign / "cfg-a" / "_config" / "files"
    files.mkdir(parents=True)
    (files / "nav2_params.yaml").write_text("controller: {max_vel: 0.4}\n")
    config = Campaign(str(campaign)).config("cfg-a")
    assert config.yaml("files/nav2_params.yaml") == {"controller": {"max_vel": 0.4}}
    assert "files/nav2_params.yaml" in config.files()
    with pytest.raises(KeyError):
        Campaign(str(campaign)).config("nope")
