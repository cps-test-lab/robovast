# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""What the host container sends back, and what it must not.

The pod's filesystem does not outlive it, so anything the host stage derived exists
nowhere until it is uploaded -- and the campaign it derived it from was staged into the
same tree. The upload therefore has to tell one from the other: everything new or changed
is output, everything untouched is the store's own copy of itself.

That "everything new" rule is right for what the stages produce and wrong for scratch, so
the exceptions are pinned here rather than left to the diff.
"""

import pytest

from robovast.execution.cluster_execution import in_pod_storage, postprocess_host
from robovast.execution.cluster_execution.postprocess_job import OUTPUT_MANIFEST

CACHE = ".robovast_rosbags_process_cache"


def _payload(store):
    """What was uploaded, less the manifest that names it.

    The manifest is written by the upload itself and rides up with it, so every assertion
    about *what* was sent would otherwise carry it. What it holds is pinned on its own,
    below.
    """
    return [k for k in store.files if not k.endswith(OUTPUT_MANIFEST)]


class _Store:
    """Records what it was asked to send, wholesale and per file."""

    def __init__(self):
        self.files = []
        self.dirs = []

    def upload_file(self, path, bucket, key):
        self.files.append(key)

    def upload_dir(self, local_dir, bucket, prefix=""):
        self.dirs.append(prefix)
        return 0


@pytest.fixture
def store(monkeypatch):
    client = _Store()
    monkeypatch.setattr(in_pod_storage, "campaign_storage_location",
                        lambda cfg, cid: ("bucket", "camp/"))
    monkeypatch.setattr(in_pod_storage, "storage_client_for", lambda cfg: client)
    return client


@pytest.fixture
def campaign(tmp_path):
    """A staged campaign as the host container finds it: run data and a bag, no outputs."""
    root = tmp_path / "camp"
    (root / "_execution").mkdir(parents=True)
    (root / "_execution" / "postprocessing.log").write_text("staged\n")
    bag = root / "cfg" / "0" / "rosbag2"
    bag.mkdir(parents=True)
    (bag / "rosbag2_0.mcap").write_bytes(b"bag bytes")
    return root


def test_the_provenance_marker_is_uploaded(store, campaign):
    """``_transient/postprocessing.yaml`` is the proof that the ingest ran.

    Every reader asks the campaign whether it has been postprocessed by looking for that
    file, so a campaign whose marker stayed in the pod reads back as un-postprocessed --
    and gets postprocessed again, forever, while its tables are already complete.
    """
    before = postprocess_host._snapshot(str(campaign))
    (campaign / "_transient").mkdir()
    (campaign / "_transient" / "postprocessing.yaml").write_text("entries: []\n")

    assert postprocess_host._upload_derived(object(), "camp", str(campaign), before) == 1
    assert _payload(store) == ["camp/_transient/postprocessing.yaml"]
    # `_execution/` goes wholesale rather than by diff: it is the campaign's account of
    # itself, and the POSTPROCESSING section of the campaign log IS a file in it.
    assert store.dirs == ["camp/_execution"]


def test_the_staged_rosbags_are_not_written_back_over_themselves(store, campaign):
    """They came FROM the store, unchanged, and they are the bulk of a campaign.

    Uploading them back would make every postprocess re-upload the campaign it was
    postprocessing -- the transfer paid twice, in the direction where it also risks
    replacing a good object with a copy of itself.
    """
    before = postprocess_host._snapshot(str(campaign))
    (campaign / "cfg" / "0" / "poses.csv").write_text("x,y\n")

    postprocess_host._upload_derived(object(), "camp", str(campaign), before)

    assert _payload(store) == ["camp/cfg/0/poses.csv"]


def test_the_per_bag_hash_cache_is_not_campaign_data(store, campaign):
    """It is new, so the diff calls it output -- and it is scratch, so it is not.

    ``rosbags_process`` keeps this cache beside the bag it describes, at a path fixed in
    the script. It is rebuildable by definition and describes a pod that no longer exists,
    so uploading it would add a file per bag to every campaign for a cache no later reader
    can use.
    """
    before = postprocess_host._snapshot(str(campaign))
    (campaign / "cfg" / "0" / "rosbag2" / CACHE).write_text("{}")
    (campaign / "cfg" / "0" / "poses.csv").write_text("x,y\n")

    postprocess_host._upload_derived(object(), "camp", str(campaign), before)

    assert _payload(store) == ["camp/cfg/0/poses.csv"]
    assert CACHE in postprocess_host.NOT_CAMPAIGN_DATA


def test_the_upload_names_what_it_sent(store, campaign):
    """The manifest is how the service fetches these objects back.

    It reads that one key and gets exactly the objects it names, rather than listing a
    campaign prefix whose bulk is the rosbags it does not want. Written here because this is
    the only place that knows what went up: a manifest written anywhere else describes what
    something was *expected* to produce.
    """
    before = postprocess_host._snapshot(str(campaign))
    (campaign / "cfg" / "0" / "poses.csv").write_text("x,y\n")
    (campaign / "cfg" / "0" / "nav_metrics.csv").write_text("m\n")

    postprocess_host._upload_derived(object(), "camp", str(campaign), before)

    named = (campaign / "_execution" / "conversion_outputs.txt").read_text().split()
    assert sorted(named) == ["cfg/0/nav_metrics.csv", "cfg/0/poses.csv"]
    assert f"camp/{OUTPUT_MANIFEST}" in store.files


def test_the_manifest_goes_up_after_what_it_names(store, campaign):
    """Otherwise it advertises objects that are not in the store yet.

    The service fetches by manifest, so a manifest that landed first would name keys a
    concurrent read could miss -- and the failure would look like a conversion that produced
    less than it did.
    """
    before = postprocess_host._snapshot(str(campaign))
    (campaign / "cfg" / "0" / "poses.csv").write_text("x,y\n")

    postprocess_host._upload_derived(object(), "camp", str(campaign), before)

    assert store.files[-1] == f"camp/{OUTPUT_MANIFEST}"


def test_a_run_that_derived_nothing_still_says_so(store, campaign):
    """An empty manifest and a missing one are different answers.

    Missing means no Job ever said, and the fetch falls back to the legacy prefix for a
    campaign converted by an older service. Empty means a Job ran and derived nothing.
    Collapsing the two is how a batch with no metrics reads as a batch nobody asked about.
    """
    before = postprocess_host._snapshot(str(campaign))

    assert postprocess_host._upload_derived(object(), "camp", str(campaign), before) == 0

    assert (campaign / "_execution" / "conversion_outputs.txt").read_text() == ""
    assert f"camp/{OUTPUT_MANIFEST}" in store.files
