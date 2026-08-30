# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Publishing a campaign's records while it runs, not only when it finishes.

``campaign.db`` holds what a reader needs to say what a campaign *is*: its description,
who launched it, where its configuration came from, and — for a search — every parameter
set scored so far. On the cluster lane it was uploaded once, by ``finalize_campaign``, so
a campaign that never reached one had no records at all: it listed with no description, no
start time and no tallies, and nothing a restart could re-launch it from.

The rule is one line with no mode in it — publish once the campaign row exists, and again
each time a batch closes. Batch mode has one batch, so it degenerates to "at the start and
at the end"; a search gets a per-batch checkpoint out of the same call.
"""

import types

import pytest

from robovast.execution.backends import ExecutionBackend, RunOptions
from robovast.execution.cluster_execution import in_pod_storage
from robovast.execution.cluster_execution.kubernetes_backend import KubernetesBackend


class _RecordingStorage:
    def __init__(self, fail=False):
        self.uploads = []
        self._fail = fail

    def upload_file(self, local, bucket, key):
        if self._fail:
            raise RuntimeError("store unreachable")
        self.uploads.append((local, bucket, key))


@pytest.fixture(name="storage")
def _storage(monkeypatch):
    st = _RecordingStorage()
    monkeypatch.setattr(in_pod_storage, "campaign_storage_location",
                        lambda cfg, cid: ("bkt", f"{cid}/"))
    monkeypatch.setattr(in_pod_storage, "storage_client_for", lambda cfg: st)
    return st


def _backend():
    return KubernetesBackend(cluster_config=object(), namespace="ns", kube_context=None)


def _campaign(tmp_path, with_db=True):
    root = tmp_path / "camp-2026-07-17-120000"
    root.mkdir()
    if with_db:
        (root / "campaign.db").write_bytes(b"sqlite")
    return root


def test_the_local_lane_publishes_nothing(tmp_path):
    """The default is a no-op: a local campaign's records are already in their home."""
    root = _campaign(tmp_path)
    ExecutionBackend.publish_records(object(), str(root))  # must not raise


def test_only_campaign_db_goes_up(storage, tmp_path):
    """The one file, not the directory it sits in.

    This runs at every batch boundary, and the campaign root beside it holds that batch's
    results — gigabytes of them. Publishing the directory would re-upload every earlier
    batch's results per batch, which is what ``finalize_campaign`` does once, at the end.
    """
    root = _campaign(tmp_path)
    (root / "config-a").mkdir()
    (root / "config-a" / "trajectory.csv").write_text("big")

    _backend().publish_records(str(root))

    assert [key for _, _, key in storage.uploads] == \
        ["camp-2026-07-17-120000/campaign.db"]


def test_a_campaign_with_no_store_yet_publishes_nothing(storage, tmp_path):
    """Nothing to publish is not a failure — the row may not have been written yet."""
    _backend().publish_records(str(_campaign(tmp_path, with_db=False)))
    assert storage.uploads == []


def test_an_unreachable_store_does_not_end_the_campaign(monkeypatch, tmp_path):
    """Bookkeeping must never become the reason a campaign fails.

    The campaign's own result uploads go through the same client moments later; a store
    that is genuinely unreachable is reported by those, with a real error.
    """
    monkeypatch.setattr(in_pod_storage, "campaign_storage_location",
                        lambda cfg, cid: ("bkt", f"{cid}/"))
    monkeypatch.setattr(in_pod_storage, "storage_client_for",
                        lambda cfg: _RecordingStorage(fail=True))

    _backend().publish_records(str(_campaign(tmp_path)))  # must not raise


# -- the controller's call sites --------------------------------------------------------

class _CountingBackend(ExecutionBackend):
    """An ExecutionBackend that records the order of what the controller asks of it."""

    def __init__(self):
        self.order = []

    def run_batch(self, campaign_data, *, campaign_root, batch_tag, runs, options,
                  whole_campaign=False):
        self.order.append("ran")

    def publish_records(self, campaign_root):
        self.order.append("published")


def _controller(tmp_path, backend, **kw):
    from robovast.common.store import STORE_FILENAME, CampaignStore
    from robovast.execution.controller import CampaignController

    store = CampaignStore(tmp_path / "camp" / STORE_FILENAME)
    return CampaignController(
        campaign_id="camp", results_dir=str(tmp_path), runs=1, backend=backend,
        options=RunOptions(), store=store, campaign_config_dump={"version": 1},
        vast_dir=str(tmp_path), notifier=types.SimpleNamespace(
            start_heartbeat=lambda **k: None, started=lambda mode: None,
            batch_finished=lambda *a: None, campaign_finished=lambda *a, **k: None),
        **kw)


def test_the_row_is_published_before_any_compute(tmp_path):
    """Publishing at the *end* is what left an interrupted campaign blank.

    Asserted as an ordering rather than a count: the point is that a campaign whose very
    first job dies still has records behind it to be listed and re-launched from.
    """
    backend = _CountingBackend()
    _controller(tmp_path, backend, batch_campaign_data={"configs": []}).run()

    assert backend.order[0] == "published"
    assert "ran" in backend.order


def test_a_closed_batch_is_published_too(tmp_path):
    """The second half of the rule, and the half a search resumes from."""
    backend = _CountingBackend()
    _controller(tmp_path, backend, batch_campaign_data={"configs": []}).run()

    # start, then again once the batch has been recorded
    assert backend.order == ["published", "ran", "published"]
