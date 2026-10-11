# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Building every table at once, compacted, answers every query as building on first use does."""

import io
import shutil

import pytest
from click.testing import CliRunner

from robovast_data import Campaign, open_data
from robovast_data.cli import build as build_command
from robovast_data.progress import ProgressLine
from robovast_decode.build import build
from robovast_decode.tables import read_manifest, compacted_runs

QUERIES = {
    "poses": "SELECT config_name, run_id, count(*) AS n, sum(\"position.x\") AS x "
             "FROM poses GROUP BY 1, 2 ORDER BY 1, 2",
    "rosbag2_scan": "SELECT config_name, run_id, count(*) AS n FROM rosbag2_scan "
                    "GROUP BY 1, 2 ORDER BY 1, 2",
    "run_log": "SELECT config_name, run_id, count(*) AS n FROM run_log GROUP BY 1, 2 "
               "ORDER BY 1, 2",
}


@pytest.fixture
def twin(campaign, tmp_path):
    """The same campaign again, to build on first use and compare against."""
    other = tmp_path / "first-use" / campaign.name
    shutil.copytree(campaign, other)
    return other


def _answers(path, **options):
    data = Campaign(str(path), workers=1, **options)
    return {name: data.sql(sql).to_dict("records") for name, sql in QUERIES.items()}


def test_a_compacted_campaign_answers_as_one_built_on_first_use(campaign, twin):
    built = Campaign(str(campaign)).build(workers=2, progress=False)
    assert built.problems == []
    compacted = built.compacted[campaign.name].compacted
    assert {"poses", "rosbag2_scan", "run_log"} <= set(compacted)
    assert _answers(campaign) == _answers(twin)


def test_a_configuration_and_a_run_read_their_rows_of_a_compacted_file(campaign, twin):
    Campaign(str(campaign)).build(workers=2, progress=False)
    for sub in ("cfg-a", "cfg-a/1", "cfg-b/0"):
        got = open_data(str(campaign / sub), workers=1).table("poses")
        want = open_data(str(twin / sub), workers=1).table("poses")
        assert len(got) == len(want) > 0, sub
        assert sorted(got["timestamp"]) == sorted(want["timestamp"]), sub


def test_a_compacted_run_built_again_is_not_counted_twice(campaign, twin):
    Campaign(str(campaign)).build(workers=2, progress=False)
    build(str(campaign), tables=["poses"], runs=["cfg-a/1"], force=True)
    assert "cfg-a/1" not in compacted_runs(read_manifest(str(campaign)), "poses")
    assert _answers(campaign)["poses"] == _answers(twin)["poses"]


def test_building_one_configuration_leaves_the_campaign_uncompacted(campaign):
    built = open_data(str(campaign / "cfg-a"), workers=1).build(progress=False)
    assert built.compacted == {}
    manifest = read_manifest(str(campaign))
    assert all(not entry.get("campaign") for entry in manifest["tables"].values())
    assert set(manifest["tables"]["poses"]["runs"]) == {"cfg-a/0", "cfg-a/1"}


def test_uncompacted_when_asked(campaign):
    built = Campaign(str(campaign)).build(workers=1, compact=False, progress=False)
    assert built.compacted == {}
    assert compacted_runs(read_manifest(str(campaign)), "poses") == set()


def test_the_progress_line_is_one_line_of_tenths():
    out = io.StringIO()
    line = ProgressLine("c: 3 runs, 2 workers", stream=out)
    for done in range(1, 4):
        line("build", done, 3)
    line("compact", 1, 2)
    line("compact", 2, 2)
    line.finish()
    text = out.getvalue()
    assert text.count("\n") == 1 and "\r" not in text
    assert text.startswith("c: 3 runs, 2 workers  0%...10%...20%")
    assert "...90%...100%  " in text and text.endswith(" s\n")


def test_the_command_builds_compacts_and_summarises(campaign):
    result = CliRunner().invoke(build_command, [str(campaign), "--jobs", "2"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "3 runs, 2 workers  0%..." in out and "...100%" in out
    assert " tables, " in out and " in .cache (recordings " in out
    assert compacted_runs(read_manifest(str(campaign)), "poses") == {"cfg-a/0", "cfg-a/1",
                                                                   "cfg-b/0"}


def test_the_command_refuses_zero_jobs(campaign):
    result = CliRunner().invoke(build_command, [str(campaign), "--jobs", "0"])
    assert result.exit_code == 2 and "--jobs" in result.output
