# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A campaign built in parts and merged answers as one built whole."""

import os
import shutil

import pytest

from robovast.results_processing.table_parts import (build_part, merge_parts, part_skip,
                                                     plan_parts, read_part, write_part)
from robovast_data import Campaign
from robovast_decode.tables import read_manifest, compacted_runs
from tests.robovast_data.conftest import nav_campaign

RUNS = (("cfg-a", 0), ("cfg-a", 1), ("cfg-a", 2), ("cfg-b", 0), ("cfg-b", 1))
QUERIES = {
    "poses": "SELECT config_name, run_id, count(*) n, sum(\"position.x\") x FROM poses "
             "GROUP BY 1, 2 ORDER BY 1, 2",
    "rosbag2_scan": "SELECT config_name, run_id, count(*) n FROM rosbag2_scan GROUP BY 1, 2 "
                    "ORDER BY 1, 2",
    "run_log": "SELECT config_name, run_id, count(*) n FROM run_log GROUP BY 1, 2 ORDER BY 1, 2",
}


@pytest.fixture
def campaign(tmp_path):
    return nav_campaign(tmp_path / "whole" / "nav-2026-01-01-00000000", runs=RUNS)


def _stage(campaign, name, into):
    """What a pod staging part *name* receives: the campaign minus what the part skips."""
    skip = part_skip(str(campaign), name)
    target = into / campaign.name
    for dirpath, dirnames, filenames in os.walk(campaign):
        rel_dir = os.path.relpath(dirpath, campaign)
        rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
        kept = []
        for d in dirnames:
            rel = f"{rel_dir}/{d}" if rel_dir else d
            full = os.path.join(dirpath, d)
            if skip(rel):
                continue
            if os.path.islink(full):
                os.makedirs(target / rel_dir, exist_ok=True)
                os.symlink(os.readlink(full), target / rel)
                continue
            kept.append(d)
        dirnames[:] = kept
        for f in filenames:
            rel = f"{rel_dir}/{f}" if rel_dir else f
            if not skip(rel):
                os.makedirs(target / rel_dir, exist_ok=True)
                full = os.path.join(dirpath, f)
                if os.path.islink(full):
                    os.symlink(os.readlink(full), target / rel)
                else:
                    shutil.copy2(full, target / rel)
    return target


def _deliver(out, campaign):
    for dirpath, _dirs, filenames in os.walk(out):
        for f in filenames:
            src = os.path.join(dirpath, f)
            dst = campaign / os.path.relpath(src, out)
            os.makedirs(dst.parent, exist_ok=True)
            shutil.copy2(src, dst)


def _answers(path):
    data = Campaign(str(path), workers=1)
    return {name: data.sql(sql).to_dict("records") for name, sql in QUERIES.items()}


def test_parts_hold_whole_units_within_the_budget(campaign):
    parts = plan_parts(str(campaign), runs_per_part=2)
    assert [p.runs for p in parts] == [["cfg-a/0", "cfg-a/1"], ["cfg-a/2", "cfg-b/0"],
                                       ["cfg-b/1"]]
    assert all(len(p.jobs) == len(p.runs) for p in parts)
    assert [p.runs for p in plan_parts(str(campaign), runs_per_part=1)] == [
        [f"{c}/{r}"] for c, r in RUNS]
    with pytest.raises(ValueError):
        plan_parts(str(campaign), runs_per_part=0)


def test_a_part_stages_its_runs_and_nothing_of_another(campaign, tmp_path):
    part = plan_parts(str(campaign), runs_per_part=2)[1]
    write_part(str(campaign), part)
    assert read_part(str(campaign), part.name).runs == part.runs
    staged = _stage(campaign, part.name, tmp_path / "stage")
    assert sorted(p.name for p in (staged / "cfg-a").iterdir() if p.name.isdigit()) == ["2"]
    assert (staged / "campaign.db").exists()
    assert not (staged / "_execution" / "table_parts").exists()
    with pytest.raises(KeyError):
        part_skip(str(campaign), "part-9")


def test_parts_built_apart_and_merged_answer_as_the_whole(campaign, tmp_path):
    whole = tmp_path / "reference" / campaign.name
    shutil.copytree(campaign, whole, symlinks=True)
    Campaign(str(whole)).build(workers=1, progress=False)

    parts = plan_parts(str(campaign), runs_per_part=2)
    for part in parts:
        write_part(str(campaign), part)
    for part in parts:
        staged = _stage(campaign, part.name, tmp_path / "stage" / part.name)
        out = build_part(str(staged), None, workers=1, generation=0, name=part.name,
                         out=str(tmp_path / "out" / part.name))
        _deliver(out, campaign)
    report = merge_parts(str(campaign), [(0, p.name) for p in parts])
    assert report.compacted["poses"] == len(RUNS)
    manifest = read_manifest(str(campaign))
    assert compacted_runs(manifest, "poses") == {f"{c}/{r}" for c, r in RUNS}
    assert not (campaign / ".cache" / "parts" / "0").exists()
    assert _answers(campaign) == _answers(whole)


def test_a_merge_with_a_part_missing_refuses(campaign):
    parts = plan_parts(str(campaign), runs_per_part=2)
    with pytest.raises(FileNotFoundError, match="part-1"):
        merge_parts(str(campaign), [(0, p.name) for p in parts])
