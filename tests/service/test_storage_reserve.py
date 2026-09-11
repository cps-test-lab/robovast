# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""New disk-consuming work is refused below the free-space reserve; running work is not.

A campaign, a re-run, an image build, an import and a postprocessing run each write an amount
nobody knows beforehand. Started on a nearly full disk, one of them does not only fail itself:
on a cluster it drives the node past the kubelet's eviction threshold and every pod there is
evicted, the service included. So the service keeps a reserve -- and the refusal, the meters
and the MCP tool must be one measurement, or a caller is refused while the meter looks fine.
"""

from collections import namedtuple

import pytest
from fastapi.testclient import TestClient

from robovast.common.errors import InsufficientStorageError
from robovast.service import storage_reserve
from robovast.service.app import build_app
from robovast.service.interface import (DiskSpace, ImportCampaignRequest, ResourceUsage,
                                        RunPostprocessingRequest, Routes)
from robovast.service.local_transport import LocalTransport
from robovast.service.storage_reserve import RESERVE_ENV, reserve_gb, storage_refusal
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

def test_an_unset_reserve_keeps_none(monkeypatch):
    """Only a disk's operator knows its eviction threshold; a fixed default would refuse every
    campaign on a disk smaller than it -- a laptop's, a CI runner's."""
    monkeypatch.delenv(RESERVE_ENV, raising=False)
    assert reserve_gb() == 0
    assert storage_refusal(_usage(disk_free_gb=1)) is None


def test_a_stated_reserve_is_read_in_gigabytes(monkeypatch):
    monkeypatch.setenv(RESERVE_ENV, "150")
    assert reserve_gb() == 150


@pytest.mark.parametrize("value", ["lots", "-5", "nan", "inf", "150GB"])
def test_a_malformed_reserve_fails_naming_the_variable(monkeypatch, value):
    """Falling back to none would leave unprotected the disk its operator meant to protect."""
    monkeypatch.setenv(RESERVE_ENV, value)
    with pytest.raises(ValueError, match=RESERVE_ENV):
        reserve_gb()


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
    assert KNOWN[storage_reserve.RESERVE_ENV].default == "0"


def test_no_reserve_reads_nothing(transport, monkeypatch):
    """Unset, the admission costs nothing -- on a cluster a reading is several API calls."""
    monkeypatch.delenv(RESERVE_ENV)

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
