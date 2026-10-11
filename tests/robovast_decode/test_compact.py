# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A finished campaign's tables are merged into one file each, and no row changes."""

import json
import os

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from robovast_decode.build import build, find_runs
from robovast_decode.layout import decoder_config
from robovast_decode.compact import compact
from robovast_decode.tables import (CONTEXT_COLUMNS, RunCatalog, cache_root, merge_fragments,
                                    read_manifest, read_run_table, run_fragment, compacted_runs)

from .conftest import make_campaign

RUNS = (("cfg-a", 0), ("cfg-a", 1), ("cfg-b", 0))


@pytest.fixture
def campaign(tmp_path):
    root = tmp_path / "nav-2026-01-01-00000000"
    make_campaign(root, runs=RUNS)
    return root


def _rows(campaign, table):
    """Every run's rows of *table*, as the manifest says where they are."""
    manifest = read_manifest(str(campaign))
    out = {}
    for run in find_runs(str(campaign)):
        rows = read_run_table(str(campaign), manifest, table, run.key)
        out[run.key] = rows.to_pylist() if rows is not None else []
    return out


def _all_rows(campaign):
    manifest = read_manifest(str(campaign))
    return {table: _rows(campaign, table) for table in manifest["tables"]}


def _parquet_files(campaign):
    return sorted(str(p.relative_to(campaign)) for p in (campaign / ".cache").rglob("*.parquet"))


def _same_rows(before, after):
    """Equal rows, a column that was all null in a run now carrying its unified type."""
    assert before.keys() == after.keys()
    for table in before:
        for key in before[table]:
            b, a = before[table][key], after[table][key]
            assert len(b) == len(a), (table, key)
            for row_b, row_a in zip(b, a):
                assert {k: v for k, v in row_a.items() if k in row_b} == row_b, (table, key)
                assert all(v is None for k, v in row_a.items() if k not in row_b), (table, key)


def test_compacting_keeps_every_row_and_leaves_one_file_per_table(campaign):
    build(str(campaign), config=decoder_config(str(campaign)))
    before = _all_rows(campaign)
    report = compact(str(campaign))
    after = _all_rows(campaign)
    _same_rows(before, after)
    manifest = read_manifest(str(campaign))
    for table in report.compacted:
        entry = manifest["tables"][table]
        assert entry["campaign"]["compacted"] == sorted(k for k, run in entry["runs"].items()
                                                     if run.get("compacted"))
        assert all(run["files"] == [] for run in entry["runs"].values() if run.get("compacted"))
        files = [f for f in _parquet_files(campaign) if f.startswith(f".cache/tables/{table}/")]
        assert files == [os.path.join(".cache", entry["campaign"]["files"][0])], table
    assert "poses" in report.compacted and report.compacted["poses"] == len(RUNS)
    assert report.bytes_after > 0
    assert sorted(p.name for p in (campaign / ".cache" / "tables" / "poses").iterdir()) == [
        os.path.basename(read_manifest(str(campaign))["tables"]["poses"]["campaign"]["files"][0])]


def test_a_compacted_file_is_ordered_by_run_with_the_context_columns_first(campaign):
    build(str(campaign), tables=["poses"])
    compact(str(campaign))
    entry = read_manifest(str(campaign))["tables"]["poses"]["campaign"]
    rows = pq.read_table(os.path.join(cache_root(str(campaign)), entry["files"][0]))
    assert rows.column_names[:3] == list(CONTEXT_COLUMNS)
    keys = [f"{c}/{r}" for c, r in zip(rows["config_name"].to_pylist(),
                                        rows["run_id"].to_pylist())]
    assert keys == sorted(keys, key=lambda k: (k.split("/")[0], int(k.split("/")[1])))


def test_compacting_again_with_nothing_new_changes_nothing(campaign):
    build(str(campaign), tables=["poses"])
    compact(str(campaign))
    files = _parquet_files(campaign)
    report = compact(str(campaign))
    assert report.compacted == {}
    assert _parquet_files(campaign) == files


def test_a_compacted_run_built_again_is_read_from_its_own_file_and_compacted_back(campaign):
    build(str(campaign), tables=["poses"])
    compact(str(campaign))
    before = _rows(campaign, "poses")
    build(str(campaign), tables=["poses"], runs=["cfg-a/1"], force=True)
    manifest = read_manifest(str(campaign))
    assert compacted_runs(manifest, "poses") == {"cfg-a/0", "cfg-b/0"}
    assert manifest["tables"]["poses"]["runs"]["cfg-a/1"]["files"]
    assert _rows(campaign, "poses") == before
    report = compact(str(campaign))
    assert report.compacted["poses"] == 3          # and its recording report, built again too
    assert compacted_runs(read_manifest(str(campaign)), "poses") == {"cfg-a/0", "cfg-a/1", "cfg-b/0"}
    assert _rows(campaign, "poses") == before


def test_a_run_without_its_verdict_keeps_its_own_file(campaign):
    (campaign / "cfg-b" / "0" / "test.xml").unlink()
    build(str(campaign), tables=["poses"])
    compact(str(campaign))
    manifest = read_manifest(str(campaign))
    assert compacted_runs(manifest, "poses") == {"cfg-a/0", "cfg-a/1"}
    assert manifest["tables"]["poses"]["runs"]["cfg-b/0"]["files"]


def test_a_derived_table_of_a_compacted_run_is_built_from_the_compacted_inputs(campaign):
    config = decoder_config(str(campaign))
    build(str(campaign), config=config)
    derived = {t: _rows(campaign, t) for t in ("run_log", "run_clock_map")}
    compact(str(campaign))
    build(str(campaign), tables=["run_log", "run_clock_map"], runs=["cfg-a/0"], force=True,
          config=config)
    for table, rows in derived.items():
        assert _rows(campaign, table) == rows, table


def test_a_run_built_against_its_entries_in_memory_enters_what_a_file_build_does(tmp_path):
    on_file = tmp_path / "a" / "nav-2026-01-01-00000000"
    in_memory = tmp_path / "b" / "nav-2026-01-01-00000000"
    for root in (on_file, in_memory):
        make_campaign(root, runs=RUNS)
    build(str(on_file), tables=["poses", "rosbag2_collision"])
    fragments = []
    for run in find_runs(str(in_memory)):
        catalog = RunCatalog(run_fragment(read_manifest(str(in_memory)), run.key))
        build(str(in_memory), tables=["poses", "rosbag2_collision"], runs=[run.key],
              catalog=catalog)
        fragments.append(catalog.manifest)
    manifest = read_manifest(str(in_memory))
    merge_fragments(manifest, fragments)
    expected = read_manifest(str(on_file))
    assert json.dumps(manifest["tables"], sort_keys=True) == json.dumps(
        expected["tables"], sort_keys=True)
    assert manifest["schemas"] == expected["schemas"]


def test_runs_whose_columns_cannot_be_unified_are_left_and_reported(campaign):
    build(str(campaign), tables=["poses"])
    manifest = read_manifest(str(campaign))
    rel = manifest["tables"]["poses"]["runs"]["cfg-b/0"]["files"][0]
    path = os.path.join(cache_root(str(campaign)), rel)
    rows = pq.read_table(path)
    rows = rows.set_column(rows.schema.get_field_index("frame"), "frame",
                           pa.array([[1]] * rows.num_rows))
    pq.write_table(rows, path)
    report = compact(str(campaign))
    assert "poses" in report.skipped
    assert compacted_runs(read_manifest(str(campaign)), "poses") == set()
