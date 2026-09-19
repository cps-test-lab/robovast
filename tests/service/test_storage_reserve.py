# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The free-space reserve: new work is refused below it, and running downloads pause at it.

A campaign, a re-run, an image build, an import and a postprocessing run each write an amount
nobody knows beforehand. Written past what the disk can spare, one of them does not only fail
itself: on a cluster it drives the node past the kubelet's eviction threshold and every pod
there is evicted, the service included. So the service keeps a reserve -- the refusal, the
meters and the MCP tool must be one measurement, and the download of a campaign already
running must stop at the reserve rather than write through it.
"""

from collections import namedtuple

import pytest
from fastapi.testclient import TestClient

from robovast.common.errors import InsufficientStorageError
from robovast.common import disk_reserve
from robovast.service.app import build_app
from robovast.service.interface import (DiskSpace, ImportCampaignRequest, ResourceUsage,
                                        RunPostprocessingRequest, Routes)
from robovast.service.local_transport import LocalTransport
from robovast.common.disk_reserve import (DEFAULT_RESERVE_FRACTION, RESERVE_ENV,
                                          configured_reserve_gb)
from robovast.service.storage_reserve import storage_refusal
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore

_GB = 1000 ** 3


def _usage(disk_free_gb=None, store_free_gb=None, **extra) -> ResourceUsage:
    def space(free_gb):
        if free_gb is None:
            return None
        return DiskSpace(capacity_bytes=1000 * _GB, used_bytes=int((1000 - free_gb) * _GB))
    return ResourceUsage(backend="docker", cpu_capacity=4, cpu_used=1,
                         memory_capacity_bytes=8 * _GB, memory_used_bytes=_GB,
                         parallel_runs=False, disk=space(disk_free_gb),
                         store=space(store_free_gb), **extra)


# -- the setting -----------------------------------------------------------------------------

def test_an_unset_reserve_is_a_fraction_of_the_disk(monkeypatch):
    """Above the kubelet's default eviction threshold of 10%, on any size of disk: an absolute
    default would sit below it on a large disk and refuse everything on a small one."""
    monkeypatch.delenv(RESERVE_ENV, raising=False)
    assert configured_reserve_gb() is None
    assert DEFAULT_RESERVE_FRACTION > 0.10
    refusal = storage_refusal(_usage(disk_free_gb=100))       # of 1000 GB
    assert "below the 150 GB reserve" in refusal
    assert "15% of that disk" in refusal and RESERVE_ENV in refusal
    assert storage_refusal(_usage(disk_free_gb=200)) is None


def test_a_reserve_of_zero_keeps_none(monkeypatch):
    monkeypatch.setenv(RESERVE_ENV, "0")
    assert storage_refusal(_usage(disk_free_gb=1)) is None


def test_a_stated_reserve_is_read_in_gigabytes(monkeypatch):
    monkeypatch.setenv(RESERVE_ENV, "150")
    assert configured_reserve_gb() == 150


def test_the_suite_switches_off_the_variable_the_module_reads():
    """The suite-wide fixture names the variable instead of importing it; this keeps the two
    the same, or every test would silently depend on the host's free space again."""
    import os
    assert os.environ[RESERVE_ENV] == "0"


@pytest.mark.parametrize("value", ["lots", "-5", "nan", "inf", "150GB"])
def test_a_malformed_reserve_fails_naming_the_variable(monkeypatch, value):
    """Falling back to none would leave unprotected the disk its operator meant to protect."""
    monkeypatch.setenv(RESERVE_ENV, value)
    with pytest.raises(ValueError, match=RESERVE_ENV):
        configured_reserve_gb()


# -- the verdict -----------------------------------------------------------------------------

def test_a_disk_below_the_reserve_is_refused_with_the_amounts(monkeypatch):
    monkeypatch.setenv(RESERVE_ENV, "150")
    refusal = storage_refusal(_usage(disk_free_gb=90, disk_node="node-a"))
    assert "the service's disk has 90 GB free" in refusal
    assert "150 GB" in refusal and RESERVE_ENV in refusal
    # It crosses the interface: amounts, never a machine.
    assert "node-a" not in refusal


def test_a_store_below_the_reserve_is_refused_too(monkeypatch):
    """The store is often on another node: one meter can be comfortable while the other fills."""
    monkeypatch.setenv(RESERVE_ENV, "150")
    refusal = storage_refusal(_usage(disk_free_gb=900, store_free_gb=20))
    assert "the results store has 20 GB free" in refusal


def test_room_on_both_meters_refuses_nothing(monkeypatch):
    monkeypatch.setenv(RESERVE_ENV, "150")
    assert storage_refusal(_usage(disk_free_gb=151, store_free_gb=900)) is None


def test_a_meter_that_could_not_be_read_is_not_a_full_disk(monkeypatch):
    monkeypatch.setenv(RESERVE_ENV, "150")
    assert storage_refusal(_usage(disk_unavailable="could not read it")) is None


# -- one measurement: /usage carries the verdict it refuses with ----------------------------

@pytest.fixture(name="transport")
def _transport(tmp_path, monkeypatch):
    store = WorkspaceStore(registry=WorkspaceRegistry(root=tmp_path / "workspaces"))
    lt = LocalTransport(store=store)
    lt._campaigns_root = lambda: tmp_path / "results"
    monkeypatch.setenv(RESERVE_ENV, "150")
    # A refusal measures the service cache for its hint; keep that off the developer's own.
    monkeypatch.setenv("ROBOVAST_SCENE_CACHE", str(tmp_path / "scenes"))
    return lt


def _short_of_disk(transport, monkeypatch, free_gb=40):
    monkeypatch.setattr(transport, "_disk_space", lambda: (
        DiskSpace(capacity_bytes=1000 * _GB, used_bytes=(1000 - free_gb) * _GB), None))
    transport._usage_cache = None


def test_usage_carries_the_refusal_its_readings_imply(transport, monkeypatch):
    _short_of_disk(transport, monkeypatch)
    usage = transport.resource_usage()
    assert usage.storage_refusal == storage_refusal(usage)
    assert "40 GB free" in usage.storage_refusal


def test_usage_says_nothing_while_there_is_room(transport, monkeypatch):
    _short_of_disk(transport, monkeypatch, free_gb=400)
    assert transport.resource_usage().storage_refusal is None


def test_free_space_is_what_a_write_can_use_not_the_filesystems_total(transport, monkeypatch):
    """Blocks held back for root are not room a campaign can write into."""
    import psutil

    Usage = namedtuple("Usage", "total used free percent")
    monkeypatch.setattr(psutil, "disk_usage", lambda _path: Usage(1000, 600, 300, 60.0))
    disk, _ = transport._disk_space()
    assert disk.capacity_bytes - disk.used_bytes == 300


# -- every operation that takes on new work is refused, and nothing else ------------------

def _refusing(transport, monkeypatch):
    _short_of_disk(transport, monkeypatch)
    return transport.resource_usage().storage_refusal


@pytest.mark.parametrize("operation", [
    lambda t: t.create_campaign(type("R", (), {"show_gui": False, "workspace_id": "w",
                                               "config_path": "c.vast"})()),
    lambda t: t.build_image(type("R", (), {"workspace_id": "w", "config_path": "c.vast",
                                           "container": ""})()),
    lambda t: t.create_archive_upload(),
    lambda t: t.import_campaign(ImportCampaignRequest(archive_path="/nowhere.tar.gz")),
    lambda t: t.run_postprocessing(RunPostprocessingRequest(campaign_id="camp-1")),
], ids=["create_campaign", "build_image", "create_archive_upload", "import_campaign",
        "run_postprocessing"])
def test_new_disk_consuming_work_is_refused_before_it_starts(transport, monkeypatch,
                                                             operation):
    refusal = _refusing(transport, monkeypatch)
    # Refused ahead of everything else: none of these inputs exist, and a refusal that came
    # after resolving them would already have said something else.
    with pytest.raises(InsufficientStorageError) as excinfo:
        operation(transport)
    assert refusal in str(excinfo.value)


def test_a_rerun_is_refused_before_anything_is_staged(transport, monkeypatch):
    from robovast.service import retrigger

    _refusing(transport, monkeypatch)
    monkeypatch.setattr(transport, "_retrigger_source_dir", lambda cid: "/source")
    monkeypatch.setattr(retrigger, "check", lambda *a, **k: None)
    monkeypatch.setattr(transport, "_admit_retrigger", lambda *a, **k: None)

    def prepare(*args, **kwargs):
        raise AssertionError("nothing may be staged for a refused re-run")

    monkeypatch.setattr(retrigger, "prepare", prepare)
    with pytest.raises(InsufficientStorageError):
        transport.retrigger_campaign("camp-1")


def test_deleting_a_campaign_is_never_refused(transport, monkeypatch):
    """Deleting is how space is freed; refusing it would lock the disk full."""
    _refusing(transport, monkeypatch)
    try:
        transport.delete_campaign("camp-that-is-not-here")
    except InsufficientStorageError:
        pytest.fail("a delete was refused for lack of the space it would free")
    except Exception:  # noqa: BLE001 - whatever a missing campaign says, it is not this
        pass


def test_the_cluster_lane_refuses_its_own_builds_and_postprocessing(monkeypatch):
    """Both are full overrides there, so each carries the admission itself."""
    from robovast.execution.cluster_execution.cluster_service import ClusterService

    monkeypatch.setenv(RESERVE_ENV, "150")
    svc = ClusterService.__new__(ClusterService)
    monkeypatch.setattr(svc, "resource_usage",
                        lambda: _usage(storage_refusal="the store is short"), raising=False)
    with pytest.raises(InsufficientStorageError, match="the store is short"):
        svc.build_image(object())
    with pytest.raises(InsufficientStorageError, match="the store is short"):
        svc.run_postprocessing(RunPostprocessingRequest(campaign_id="camp-1"))


# -- over HTTP ----------------------------------------------------------------------------

def test_a_refused_launch_is_a_507_carrying_the_sentence(transport, monkeypatch):
    refusal = _refusing(transport, monkeypatch)
    with TestClient(build_app(transport, mount_mcp=False)) as client:
        resp = client.post(Routes.CAMPAIGN_ARCHIVES)
        usage = client.get(Routes.USAGE).json()
    assert resp.status_code == 507
    assert refusal in resp.json()["detail"]
    # What the meter said before the caller tried is what the refusal says after.
    assert usage["storage_refusal"] == refusal


def test_the_setting_is_reported_with_its_default():
    from robovast.service.settings_report import KNOWN
    assert KNOWN[disk_reserve.RESERVE_ENV].default == "15% of the disk"


def test_no_reserve_reads_nothing(transport, monkeypatch):
    """Switched off, the admission costs nothing -- on a cluster a reading is several API
    calls."""
    monkeypatch.setenv(RESERVE_ENV, "0")

    def unreadable():
        raise AssertionError("no reserve, so nothing to read")

    monkeypatch.setattr(transport, "resource_usage", unreadable)
    assert transport.create_archive_upload().token


def test_a_reading_that_fails_is_not_a_refusal(transport, monkeypatch, caplog):
    """Refusing every launch because capacity could not be read would make a campaign depend
    on a permission it never needed; like an unmeasured meter, it is not judged."""
    def unreadable():
        raise PermissionError("nodes is forbidden")

    monkeypatch.setattr(transport, "resource_usage", unreadable)
    assert transport.create_archive_upload().token
    assert "reserve was not applied" in caplog.text


def test_a_malformed_reserve_fails_the_operation_loudly(transport, monkeypatch):
    monkeypatch.setenv(RESERVE_ENV, "plenty")
    with pytest.raises(ValueError, match=RESERVE_ENV):
        transport.create_archive_upload()


# -- running work pauses at the reserve ------------------------------------------------------

class _Disk:
    """A filesystem whose free space the test sets, read the way ``psutil.disk_usage`` is."""

    Usage = namedtuple("Usage", "total used free percent")

    def __init__(self, free_gb, capacity_gb=1000):
        self.free_gb = free_gb
        self.capacity_gb = capacity_gb
        self.paths = []

    def __call__(self, path):
        self.paths.append(path)
        free = int(self.free_gb * _GB)
        return self.Usage(self.capacity_gb * _GB, self.capacity_gb * _GB - free, free, 0.0)


@pytest.fixture(name="disk")
def _disk(monkeypatch):
    import psutil

    disk = _Disk(free_gb=400)
    monkeypatch.setattr(psutil, "disk_usage", disk)
    monkeypatch.setenv(RESERVE_ENV, "150")
    return disk


def test_a_download_with_room_does_not_wait(disk, tmp_path):
    def sleep(_s):
        raise AssertionError("room enough, so nothing to wait for")

    disk_reserve.wait_for_room(tmp_path, action="Batch b's download", sleep=sleep)


def test_a_download_at_the_reserve_pauses_until_space_is_freed(disk, tmp_path, caplog):
    caplog.set_level("INFO", logger=disk_reserve.__name__)
    disk.free_gb = 90
    said = []

    def sleep(_s):
        disk.free_gb += 50              # somebody deletes a campaign
    disk_reserve.wait_for_room(tmp_path, action="Batch b's download",
                                  on_pause=said.append, sleep=sleep)
    assert disk.free_gb >= 150
    # The pause is put where a person looks, with the amounts, and taken down again.
    assert said[0].startswith("Batch b's download paused: the service's disk has 90 GB free")
    assert "150 GB reserve" in said[0]
    assert said[-1] is None
    assert "Batch b's download paused" in caplog.text
    assert "resumed" in caplog.text


def test_a_stop_ends_the_pause(disk, tmp_path):
    """A stopped campaign must not sit waiting for space it no longer needs."""
    disk.free_gb = 90
    with pytest.raises(InsufficientStorageError, match="stopped while paused"):
        disk_reserve.wait_for_room(tmp_path, action="Batch b's download",
                                      should_stop=lambda: True, sleep=lambda _s: None)


def test_the_pause_is_between_files(disk, tmp_path):
    """``download_prefix`` calls ``on_file`` after each file, so the next one waits."""
    order = []
    disk.free_gb = 90

    def sleep(_s):
        order.append("waited")
        disk.free_gb = 400

    callback = disk_reserve.pausing(tmp_path, action="a download",
                                       on_file=lambda: order.append("counted"), sleep=sleep)
    callback()
    assert order == ["counted", "waited"]


def test_the_disk_measured_is_the_one_written_to(disk, tmp_path):
    """A download creates its destination, so the nearest existing ancestor is measured."""
    disk_reserve.wait_for_room(tmp_path / "camp" / "_jobs", action="a download")
    assert disk.paths == [str(tmp_path)]


def test_a_fetch_somebody_waits_on_is_refused_not_paused(disk, tmp_path):
    disk.free_gb = 90
    with pytest.raises(InsufficientStorageError,
                       match="Cannot fetch campaign c: the service's disk has 90 GB free"):
        disk_reserve.require_room(tmp_path, action="fetch campaign c")


def test_no_reserve_never_pauses(disk, tmp_path, monkeypatch):
    monkeypatch.setenv(RESERVE_ENV, "0")
    disk.free_gb = 0
    disk_reserve.wait_for_room(tmp_path, action="a download",
                                  sleep=lambda _s: pytest.fail("no reserve, no pause"))


def test_the_cluster_backend_puts_the_pause_on_the_campaigns_status():
    """The stage is what ``get_campaign_status`` and the web UI show; the stop is the runs'."""
    from robovast.execution.cluster_execution.kubernetes_backend import _disk_pause_kwargs

    class State:
        stage = "running"
        stop_requested = False

        def update(self, **fields):
            self.stage = fields["stage"]

    state = State()
    pause = _disk_pause_kwargs(state)
    pause["on_pause"]("paused: short of disk")
    assert state.stage == "paused: short of disk"
    pause["on_pause"](None)
    assert state.stage is None
    assert pause["should_stop"]() is False
    state.stop_requested = True
    assert pause["should_stop"]() is True
    assert _disk_pause_kwargs(None) == {}


def test_postprocessing_outputs_wait_for_room_before_each_file(disk, tmp_path, monkeypatch):
    from robovast.execution.cluster_execution import in_pod_storage
    from robovast.execution.cluster_execution import postprocess_job as pj

    written = []

    class Store:
        def download_prefix(self, bucket, prefix, local_dir, force=False, on_file=None, **_):
            return 0

        def read_object(self, bucket, key):
            return b"a.csv\nb.csv\n"

        def stat_object(self, bucket, key):
            return None

        def download_object(self, bucket, key, dst):
            written.append((disk.free_gb, key.rsplit("/", 1)[-1]))
            disk.free_gb -= 30              # each output takes 30 GB
            return True

        def delete_prefix(self, bucket, prefix):
            return 0

    monkeypatch.setattr(in_pod_storage, "campaign_storage_location", lambda _c, _i: ("b", "p/"))
    monkeypatch.setattr(in_pod_storage, "storage_client_for", lambda _c: Store())
    monkeypatch.setattr(pj, "_manifest_paths", lambda _m: ["a.csv", "b.csv"])
    disk.free_gb = 170

    def sleep(_s):
        disk.free_gb = 400

    monkeypatch.setattr(disk_reserve.time, "sleep", sleep)
    pj.sync_outputs(object(), "camp", str(tmp_path))
    # a.csv fitted; b.csv would have taken the disk to 110 GB, so it waited for room first.
    assert written == [(170, "a.csv"), (400, "b.csv")]
