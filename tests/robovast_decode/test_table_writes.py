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


def test_a_run_lock_holds_a_second_builder_of_that_run_and_no_other(tmp_path):
    import threading
    import time

    from robovast_decode.tables import run_lock

    campaign = str(tmp_path)
    order = []
    first_holds = threading.Event()

    def first():
        with run_lock(campaign, "cfg/0"):
            first_holds.set()
            time.sleep(0.3)
            order.append("first done")

    def second():
        first_holds.wait()
        with run_lock(campaign, "cfg/0"):
            order.append("second in")

    def other_run():
        first_holds.wait()
        with run_lock(campaign, "cfg/1"):
            order.append("other run in")

    threads = [threading.Thread(target=f) for f in (first, second, other_run)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert order.index("first done") < order.index("second in")
    assert order.index("other run in") < order.index("first done"), \
        "a lock on one run must not hold a build of another"


def test_two_builds_of_one_run_at_once_decode_it_once(campaign):
    """What two panels opening together ask for: the second finds the first's tables current."""
    from robovast_decode.build import build

    with ThreadPoolExecutor(max_workers=2) as pool:
        reports = list(pool.map(lambda _: build(str(campaign), tables=["poses"]), range(2)))
    built = [r.built.get("poses", []) for r in reports]
    skipped = [r.skipped.get("poses", []) for r in reports]
    assert sorted(len(b) for b in built) == [0, 1], (built, skipped)
    assert sorted(len(s) for s in skipped) == [0, 1], (built, skipped)
