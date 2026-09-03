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


def _campaign(tmp_path, with_db=True, with_execution=False):
    root = tmp_path / "camp-2026-07-17-120000"
    root.mkdir()
    if with_db:
        (root / "campaign.db").write_bytes(b"sqlite")
    if with_execution:
        (root / "_execution").mkdir()
        (root / "_execution" / "launch.yaml").write_text("images: {sut: reg/sut:abc}\n")
        (root / "_execution" / "execution.yaml").write_text(
            "image_revisions: {simulation: reg/sim@sha256:beef}\n")
    return root


def test_the_local_lane_publishes_nothing(tmp_path):
    """The default is a no-op: a local campaign's records are already in their home."""
    root = _campaign(tmp_path)
    ExecutionBackend.publish_records(object(), str(root))  # must not raise


def test_only_the_record_goes_up_never_the_results(storage, tmp_path):
    """Named files, not the directory they sit in.

    This runs at every batch boundary, and the campaign root beside it holds that batch's
    results — gigabytes of them. Publishing the directory would re-upload every earlier
    batch's results per batch, which is what ``finalize_campaign`` does once, at the end.
    """
    root = _campaign(tmp_path, with_execution=True)
    (root / "config-a").mkdir()
    (root / "config-a" / "trajectory.csv").write_text("big")
    (root / "_execution" / "controller.log").write_text("still being written")

    _backend().publish_records(str(root))

    assert sorted(key for _, _, key in storage.uploads) == [
        "camp-2026-07-17-120000/_execution/execution.yaml",
        "camp-2026-07-17-120000/_execution/launch.yaml",
        "camp-2026-07-17-120000/campaign.db",
    ]


def test_the_execution_record_goes_up_while_the_campaign_runs(storage, tmp_path):
    """What a reader needs to say what a campaign is RUNNING, not only what it ran.

    ``execution.yaml`` names the pinned image of every role, and is written before the jobs
    precisely so a campaign that dies in its first batch still names the bytes it ran. On
    this lane the driver's disk is scratch, so leaving it there records that for nobody —
    and a reader of a live campaign (which packages must this run's world be built from?)
    finds nothing at all until the batch ends, which for one batch is the whole campaign.
    """
    _backend().publish_records(str(_campaign(tmp_path, with_db=False, with_execution=True)))

    assert [key for _, _, key in storage.uploads] == [
        "camp-2026-07-17-120000/_execution/launch.yaml",
        "camp-2026-07-17-120000/_execution/execution.yaml",
    ]


def test_a_record_file_not_written_yet_is_skipped_not_failed(storage, tmp_path):
    """Each file appears at its own moment; the store is never asked for one that is absent."""
    _backend().publish_records(str(_campaign(tmp_path)))

    assert [key for _, _, key in storage.uploads] == ["camp-2026-07-17-120000/campaign.db"]


def test_a_campaign_with_nothing_written_yet_publishes_nothing(storage, tmp_path):
    """Nothing to publish is not a failure — the first call runs before anything exists."""
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
