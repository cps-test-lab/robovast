# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Writing one table from several threads at once, as two requests building it together do."""

import os
from concurrent.futures import ThreadPoolExecutor

import pyarrow as pa
import pyarrow.parquet as pq

from robovast_decode.tables import cache_root, write_manifest, write_table


def test_writers_of_the_same_table_do_not_rename_each_others_files(tmp_path):
    """Each writer has its own temporary file, so the last rename wins and none fails."""
    campaign = str(tmp_path)
    table = pa.table({"x": list(range(1000))})

    def write(_):
        return write_table(campaign, "costmaps/goal-1/0.parquet", table)

    with ThreadPoolExecutor(max_workers=8) as pool:
        sizes = list(pool.map(write, range(32)))
    path = os.path.join(cache_root(campaign), "costmaps/goal-1/0.parquet")
    assert all(size > 0 for size in sizes)
    assert pq.read_table(path).num_rows == 1000
    left = [n for n in os.listdir(os.path.dirname(path)) if n.endswith(".incoming")]
    assert not left, f"temporary files left behind: {left}"


def test_writers_of_the_manifest_leave_one_whole_file(tmp_path):
    campaign = str(tmp_path)

    def write(n):
        write_manifest(campaign, {"version": n, "tables": {}})

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(32)))
    root = cache_root(campaign)
    assert not [n for n in os.listdir(root) if n.endswith(".incoming")]
