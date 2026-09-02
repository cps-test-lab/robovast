# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""How many bags a conversion opens at once, and where that number comes from.

The step converts one bag per process, so the fan-out has to follow the CPU the process
may actually use -- not the CPU the machine happens to have.
"""

import pytest

from robovast.results_processing.data import rosbags_common


def _cgroup_v2(tmp_path, monkeypatch, contents: str):
    """Point the v2 quota read at *contents*, and leave v1 absent."""
    (tmp_path / "cpu.max").write_text(contents, encoding="utf-8")
    real_open = open

    def _fake_open(path, *args, **kwargs):
        if str(path) == "/sys/fs/cgroup/cpu.max":
            return real_open(tmp_path / "cpu.max", *args, **kwargs)
        if str(path).startswith("/sys/fs/cgroup/cpu/"):
            raise FileNotFoundError(path)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", _fake_open)


@pytest.mark.parametrize("quota,expected", [
    ("400000 100000", 4),
    ("100000 100000", 1),
    # A fractional quota still runs; it runs one bag at a time.
    ("50000 100000", 1),
    # Rounded up, so an allocation of 3.5 cores is not treated as 3.
    ("350000 100000", 4),
])
def test_the_budget_comes_from_the_cgroup_quota(tmp_path, monkeypatch, quota, expected):
    """``os.cpu_count()`` reports the machine's cores, which in a container is not an answer
    to how much this process may use. Sizing a pool from it puts a worker per core of the
    machine inside an allocation of a few: they contend instead of converting, and each holds
    a bag's messages while it waits, so the memory peak follows the machine too."""
    _cgroup_v2(tmp_path, monkeypatch, quota)
    assert rosbags_common.available_cpus() == expected


def test_a_v1_host_is_read_too(tmp_path, monkeypatch):
    """Both cgroup layouts, because the container does not choose which one it lands on."""
    (tmp_path / "cpu.cfs_quota_us").write_text("200000", encoding="utf-8")
    (tmp_path / "cpu.cfs_period_us").write_text("100000", encoding="utf-8")
    real_open = open

    def _fake_open(path, *args, **kwargs):
        name = str(path)
        if name == "/sys/fs/cgroup/cpu.max":
            raise FileNotFoundError(name)
        if name.startswith("/sys/fs/cgroup/cpu/"):
            return real_open(tmp_path / name.rsplit("/", 1)[1], *args, **kwargs)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", _fake_open)
    assert rosbags_common.available_cpus() == 2


def test_an_unquotaed_cgroup_falls_back_to_the_affinity_mask(tmp_path, monkeypatch):
    """``max`` is the literal the kernel writes for "no quota" -- not a number, and not a
    reason to claim the whole machine when an affinity mask still bounds us."""
    _cgroup_v2(tmp_path, monkeypatch, "max 100000")
    monkeypatch.setattr("os.sched_getaffinity", lambda _pid: {0, 1, 2})
    assert rosbags_common.available_cpus() == 3


def test_the_budget_is_never_zero(tmp_path, monkeypatch):
    """A zero worker count converts nothing and reports success, which is the worst shape
    this can take: the campaign's metrics are simply absent, with no failure to look at."""
    _cgroup_v2(tmp_path, monkeypatch, "0 100000")
    monkeypatch.setattr("os.sched_getaffinity", lambda _pid: set())
    monkeypatch.setattr("os.cpu_count", lambda: None)
    assert rosbags_common.available_cpus() == 1


def test_the_discovery_pool_is_bounded_by_the_budget_too(tmp_path, monkeypatch):
    """Bag discovery multiplied the same mistake: a thread multiple of the machine's cores.

    It stays a multiple -- the scan blocks on the store far more than it computes -- but of
    the budget, and capped, so a large allocation cannot open more concurrent reads than a
    store answers well.
    """
    from concurrent.futures import ThreadPoolExecutor

    seen = {}

    class _Executor(ThreadPoolExecutor):
        def __init__(self, max_workers=None, **kwargs):
            seen["max_workers"] = max_workers
            super().__init__(max_workers=max_workers, **kwargs)

    monkeypatch.setattr(rosbags_common, "ThreadPoolExecutor", _Executor)
    monkeypatch.setattr(rosbags_common, "available_cpus", lambda: 2)
    (tmp_path / "run-1").mkdir()

    rosbags_common.find_rosbags(str(tmp_path))

    assert seen["max_workers"] == 8
