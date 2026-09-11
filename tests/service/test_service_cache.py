# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Clearing the service's caches frees what nothing may still use, and says what it kept.

The caches are copies of durable data -- a campaign's files fetched from the object store,
compiled worlds -- so a clear costs only rebuild time. What makes it safe to offer as a button
is what it refuses to touch: a running campaign's files, a dir an operation holds, and one a
reader was handed recently enough that it may still be opening files under it. Each kept entry
is named with its reason, so "nothing to free" and "in use" read differently.
"""

import os
import threading
import time

import pytest
from fastapi.testclient import TestClient

from robovast.common.errors import InsufficientStorageError
from robovast.service import scene_cache
from robovast.service.app import build_app
from robovast.service.interface import DiskSpace, Routes
from robovast.service.local_transport import SCENE_CACHE, LocalTransport
from robovast.service.storage_reserve import RESERVE_ENV
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore


def _entry(root, name, size, *, idle_s=0.0):
    """A cache entry holding *size* bytes -- sparse, so a gigabyte costs no disk -- last read
    *idle_s* ago. The dir's own modification time is that record."""
    path = root / name
    path.mkdir(parents=True)
    with open(path / "blob", "wb") as handle:
        handle.truncate(size)
    then = time.time() - idle_s
    os.utime(path, (then, then))
    return path


#: Idle long enough that the one-hour read grace no longer protects an entry.
_UNREAD = 2 * 3600.0


@pytest.fixture(name="scenes")
def _scenes(tmp_path, monkeypatch):
    root = tmp_path / "scenes"
    root.mkdir()
    monkeypatch.setenv("ROBOVAST_SCENE_CACHE", str(root))
    return root


@pytest.fixture(name="transport")
def _transport(tmp_path, scenes):
    store = WorkspaceStore(registry=WorkspaceRegistry(root=tmp_path / "workspaces"))
    lt = LocalTransport(store=store)
    lt._campaigns_root = lambda: tmp_path / "results"
    return lt


# -- the scene cache, on every lane -----------------------------------------------------------

def test_a_report_measures_and_removes_nothing(transport, scenes):
    _entry(scenes, "world-a", 1000)
    _entry(scenes, "world-b", 500)

    report = transport.service_cache()

    assert [(c.name, c.size_bytes, c.entries) for c in report.caches] == [(SCENE_CACHE, 1500, 2)]
    assert (report.freed_bytes, report.removed_entries) == (0, 0)
    assert (scenes / "world-a").is_dir() and (scenes / "world-b").is_dir()


def test_a_clear_frees_every_entry_no_viewer_is_loading(transport, scenes):
    _entry(scenes, "world-a", 1000)
    _entry(scenes, "world-b", 500)
    lock = scene_cache._lock_for("world-b")
    with lock:
        cleared = transport.clear_service_cache()

    assert (cleared.freed_bytes, cleared.removed_entries) == (1000, 1)
    assert not (scenes / "world-a").exists()
    assert (scenes / "world-b").is_dir()
    assert [(k.name, k.size_bytes) for k in cleared.kept] == [("world-b", 500)]
    assert cleared.caches[0].size_bytes == 500, "caches report what remains"


def test_the_local_lane_offers_no_cache_of_its_results(transport, tmp_path):
    """Its results directory is the campaigns' durable home, not a copy of one."""
    (tmp_path / "results" / "camp-1").mkdir(parents=True)
    assert [c.name for c in transport.clear_service_cache().caches] == [SCENE_CACHE]
    assert (tmp_path / "results" / "camp-1").is_dir()


# -- the fetch cache, on the cluster lane ------------------------------------------------------

@pytest.fixture(name="cluster")
def _cluster(tmp_path, scenes, monkeypatch):
    from robovast.execution.cluster_execution.cluster_service import ClusterService

    svc = ClusterService.__new__(ClusterService)
    svc._lock = threading.Lock()
    svc._campaigns = {}
    svc._fetch_locks, svc._fetch_locks_guard = {}, threading.Lock()
    svc._cache_pins = {}
    monkeypatch.setattr(svc, "_FETCH_CACHE_ROOT", tmp_path / "fetched")
    return svc


def _live(campaign_id):
    from robovast.client.status import Phase
    from robovast.execution.control_server import ControllerState
    from robovast.service.local_transport import _LocalCampaign

    state = ControllerState(campaign_id=campaign_id)
    state.set_phase(Phase.RUNNING)
    return _LocalCampaign(campaign_id, "results", state)


def test_a_clear_keeps_what_may_still_be_in_use_and_says_why(cluster, tmp_path):
    root = tmp_path / "fetched"
    for cid in ("camp-idle", "camp-live", "camp-pinned", "camp-recent"):
        _entry(root, cid, 100, idle_s=_UNREAD)
    cluster._campaigns["camp-live"] = _live("camp-live")
    cluster._mark_cache_read("camp-recent")

    with cluster._holding_cache("camp-pinned"):
        cleared = cluster.clear_service_cache()

    assert (cleared.freed_bytes, cleared.removed_entries) == (100, 1)
    assert not (root / "camp-idle").exists()
    reasons = {k.name: k.reason for k in cleared.kept if k.cache == cluster.FETCH_CACHE}
    assert set(reasons) == {"camp-live", "camp-pinned", "camp-recent"}
    assert "running" in reasons["camp-live"]
    assert "operation" in reasons["camp-pinned"]
    assert "read within the last hour" in reasons["camp-recent"]


def test_a_read_long_ago_no_longer_protects_a_dir(cluster, tmp_path):
    _entry(tmp_path / "fetched", "camp-old", 100, idle_s=cluster._CACHE_READ_GRACE_S + 1)
    assert cluster.clear_service_cache().removed_entries == 1


def test_a_read_is_recorded_on_the_dir_so_a_restart_keeps_it(cluster, tmp_path):
    """A service restarted beside a cache it did not fill must still know what was read."""
    _entry(tmp_path / "fetched", "camp-1", 100, idle_s=_UNREAD)
    cluster._mark_cache_read("camp-1")
    assert cluster._cache_idle_s("camp-1") < 5


def test_a_pin_is_released_when_the_operation_ends(cluster, tmp_path):
    _entry(tmp_path / "fetched", "camp-1", 100, idle_s=_UNREAD)
    with cluster._holding_cache("camp-1"):
        with cluster._holding_cache("camp-1"):
            pass
        assert cluster.clear_service_cache().removed_entries == 0
    assert cluster.clear_service_cache().removed_entries == 1


def test_a_fetch_in_flight_finishes_and_its_reader_is_then_kept(cluster, tmp_path):
    """A clear waits on the campaign's fetch lock and checks again: the fetch it waited for
    marked the dir read, so its reader -- about to open files there -- keeps it."""
    _entry(tmp_path / "fetched", "camp-1", 100, idle_s=_UNREAD)
    lock = cluster._fetch_locks.setdefault("camp-1", threading.Lock())
    lock.acquire()
    result = {}
    clearing = threading.Thread(target=lambda: result.update(r=cluster.clear_service_cache()))
    clearing.start()
    time.sleep(0.2)                      # the clear has judged the dir free and now waits
    cluster._mark_cache_read("camp-1")   # what the fetch holding the lock does at its end
    lock.release()
    clearing.join(5)

    assert result["r"].removed_entries == 0
    assert (tmp_path / "fetched" / "camp-1").is_dir()


# -- the refusal points at the cache when a clear would help ------------------------------------

def _refusing(transport, monkeypatch):
    monkeypatch.setenv(RESERVE_ENV, "150")
    monkeypatch.setattr(transport, "_disk_space", lambda: (
        DiskSpace(capacity_bytes=1000 * 1000 ** 3, used_bytes=990 * 1000 ** 3), None))
    transport._usage_cache = None


def test_a_refusal_suggests_the_cache_when_clearing_it_would_free_enough(transport,
                                                                        monkeypatch):
    _refusing(transport, monkeypatch)
    monkeypatch.setattr(transport, "_clearable_cache_bytes", lambda: 12 * 1000 ** 3)
    with pytest.raises(InsufficientStorageError) as excinfo:
        transport.create_archive_upload()
    assert "Clearing the service cache frees 12 GB" in str(excinfo.value)
    assert excinfo.value.next_step == "vast service cache --clear"


def test_a_refusal_does_not_send_the_caller_to_a_cache_that_would_free_little(transport,
                                                                            monkeypatch):
    _refusing(transport, monkeypatch)
    monkeypatch.setattr(transport, "_clearable_cache_bytes", lambda: 10 * 1000 ** 2)
    with pytest.raises(InsufficientStorageError) as excinfo:
        transport.create_archive_upload()
    assert "cache" not in str(excinfo.value)
    assert not excinfo.value.next_step


# -- every surface -----------------------------------------------------------------------------

def test_the_routes_report_and_clear(transport, scenes):
    _entry(scenes, "world-a", 1000)
    with TestClient(build_app(transport, mount_mcp=False)) as client:
        report = client.get(Routes.ADMIN_CACHE)
        cleared = client.delete(Routes.ADMIN_CACHE)
    assert report.status_code == 200 and cleared.status_code == 200
    assert report.json()["caches"][0]["size_bytes"] == 1000
    assert cleared.json()["freed_bytes"] == 1000


def test_the_cli_reports_and_clears(transport, scenes, monkeypatch):
    import contextlib

    from click.testing import CliRunner

    from robovast.client import service_cli

    _entry(scenes, "world-a", 2_000_000_000)
    monkeypatch.setattr(service_cli, "service_client",
                        lambda namespace, context: contextlib.nullcontext((transport, "here")))
    shown = CliRunner().invoke(service_cli.service, ["cache"])
    cleared = CliRunner().invoke(service_cli.service, ["cache", "--clear"])
    assert "scene cache" in shown.output and "2.0 GB in 1 entry" in shown.output
    assert "freed 2.0 GB (1 entry)" in cleared.output


# -- expiry: what nobody has read for the maximum age goes on its own --------------------------

_WEEK = 7 * 86400.0


def test_a_dir_unread_past_the_maximum_age_expires(cluster, tmp_path):
    root = tmp_path / "fetched"
    _entry(root, "camp-stale", 100, idle_s=_WEEK + 60)
    _entry(root, "camp-this-week", 100, idle_s=_WEEK - 60)

    assert cluster._expire_fetch_cache(_WEEK) == 1
    assert not (root / "camp-stale").exists()
    assert (root / "camp-this-week").is_dir()


def test_expiry_keeps_what_a_clear_keeps(cluster, tmp_path):
    """Old is not enough: a running campaign's files and a pinned dir stay, however stale."""
    root = tmp_path / "fetched"
    _entry(root, "camp-live", 100, idle_s=_WEEK + 60)
    _entry(root, "camp-pinned", 100, idle_s=_WEEK + 60)
    cluster._campaigns["camp-live"] = _live("camp-live")

    with cluster._holding_cache("camp-pinned"):
        assert cluster._expire_fetch_cache(_WEEK) == 0
    assert (root / "camp-live").is_dir() and (root / "camp-pinned").is_dir()


def test_a_read_landing_while_expiry_waits_saves_the_dir(cluster, tmp_path):
    _entry(tmp_path / "fetched", "camp-1", 100, idle_s=_WEEK + 60)
    lock = cluster._fetch_locks.setdefault("camp-1", threading.Lock())
    lock.acquire()
    result = {}
    expiring = threading.Thread(target=lambda: result.update(n=cluster._expire_fetch_cache(_WEEK)))
    expiring.start()
    time.sleep(0.2)                      # judged stale, now waiting for the fetch to finish
    cluster._mark_cache_read("camp-1")
    lock.release()
    expiring.join(5)

    assert result["n"] == 0
    assert (tmp_path / "fetched" / "camp-1").is_dir()


def test_expiry_runs_on_its_own_and_stops_with_the_service(cluster, monkeypatch):
    from robovast.execution.cluster_execution.fetch_cache import MAX_AGE_ENV

    monkeypatch.setenv(MAX_AGE_ENV, "7")
    cluster._cache_expiry, cluster._cache_expiry_stop = None, threading.Event()
    passes = threading.Event()
    seen = {}

    def expire(max_age_s):
        seen["max_age_s"] = max_age_s
        passes.set()
        return 0

    monkeypatch.setattr(cluster, "_expire_fetch_cache", expire)
    cluster._start_cache_expiry()
    assert passes.wait(5), "the first pass runs at start, not an interval later"
    assert seen["max_age_s"] == _WEEK

    cluster._cache_expiry_stop.set()
    cluster._cache_expiry.join(5)
    assert not cluster._cache_expiry.is_alive()


def test_a_maximum_age_of_zero_starts_nothing(cluster, monkeypatch):
    from robovast.execution.cluster_execution.fetch_cache import MAX_AGE_ENV

    monkeypatch.setenv(MAX_AGE_ENV, "0")
    cluster._cache_expiry, cluster._cache_expiry_stop = None, threading.Event()
    cluster._start_cache_expiry()
    assert cluster._cache_expiry is None


def test_the_maximum_age_defaults_to_a_week_and_refuses_nonsense(monkeypatch):
    from robovast.execution.cluster_execution import fetch_cache

    monkeypatch.delenv(fetch_cache.MAX_AGE_ENV, raising=False)
    assert fetch_cache.max_age_days() == 7
    monkeypatch.setenv(fetch_cache.MAX_AGE_ENV, "2.5")
    assert fetch_cache.max_age_days() == 2.5
    for bad in ("a week", "-1", "nan"):
        monkeypatch.setenv(fetch_cache.MAX_AGE_ENV, bad)
        with pytest.raises(ValueError, match=fetch_cache.MAX_AGE_ENV):
            fetch_cache.max_age_days()
