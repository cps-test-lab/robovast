# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""On the cluster, reading the frozen config reads the frozen config.

The third member of the family with ``test_cluster_query_dbs`` (a query fetches two
databases) and ``test_cluster_file_reads`` (a file read fetches one object). This one
pins the *cheap config readers* — declared plots, panel assets, visualization workloads
— to the ``_config`` snapshot.

They were not cheap. Each went through the inherited ``_data_dir``, which on this lane
answered ``fetch_campaign``: the whole object-store prefix, every rosbag, to read one
small ``.vast``. ``list_campaign_plots`` is called **per campaign** by the Results page,
so opening the UI against a cluster holding a few large campaigns moved gigabytes into
the pod to render a list of plot names. Nothing errored; it was merely slow, which is
why it survived.

The guard is structural rather than per-method: ``ClusterService._data_dir`` now raises,
so a future method that reaches for it fails loudly here instead of quietly moving a
terabyte in production.
"""

import threading

import pytest

from robovast.execution.cluster_execution.cluster_service import ClusterService


class _FakeStorage:
    """Serves named objects; refuses the whole-prefix pull outright."""

    def __init__(self, objects):
        self.objects = objects
        self.calls = []

    def list_entries(self, bucket, prefix="", delimited=False):
        self.calls.append(("list_entries", prefix))
        clean = prefix.rstrip("/")
        key_prefix = f"{clean}/" if clean else ""
        return ([(key, len(data)) for key, data in self.objects.items()
                 if key.startswith(key_prefix)], [])

    def stat_object(self, bucket, key):
        self.calls.append(("stat_object", key))
        data = self.objects.get(key)
        return None if data is None else len(data)

    def download_object(self, bucket, key, dst):
        self.calls.append(("download_object", key))
        data = self.objects.get(key)
        if data is None:
            return False
        with open(dst, "wb") as fh:
            fh.write(data)
        return True

    def download_prefix(self, *a, **kw):                       # pragma: no cover
        raise AssertionError("reading the config must not fetch the whole campaign")


PLOTS_VAST = b"""\
visualization:
  results:
    data_browser:
      plots:
        - title: speed
          query: SELECT time, speed FROM runs
"""


@pytest.fixture(name="svc")
def _svc(monkeypatch, tmp_path):
    storage = _FakeStorage({
        "camp-1/_config/campaign.vast": PLOTS_VAST,
        "camp-1/_config/panel/bundle.js": b"console.log('panel')",
        # Present in the store, and the whole point: touching either of these means the
        # cheap reader is not cheap.
        "camp-1/_execution/run-0/rosbag.db3": b"X" * 4096,
        "camp-1/campaign.db": b"CAMPAIGN-DB",
    })
    svc = ClusterService.__new__(ClusterService)
    svc._fetch_locks = {}
    svc._fetch_locks_guard = threading.Lock()
    svc._last_fetch = {}
    svc._work_progress = {}
    svc._work_progress_guard = threading.Lock()
    monkeypatch.setattr(ClusterService, "_campaign_object_location",
                        lambda self, cid, *, interactive=False: (storage, "bucket", f"{cid}/"))
    monkeypatch.setattr(ClusterService, "_cache_dir", lambda self, cid: tmp_path / cid)
    monkeypatch.setattr(ClusterService, "fetch_campaign",
                        lambda *a, **kw: pytest.fail(
                            "a config read must never fall back to fetch_campaign"))
    return svc, storage


def _downloaded(storage):
    return {key for kind, key in storage.calls if kind == "download_object"}


def test_data_dir_is_refused_on_the_cluster_lane(svc):
    """The structural guard: the implicit whole-campaign resolver no longer answers."""
    service, _ = svc
    with pytest.raises(NotImplementedError) as excinfo:
        service._data_dir("camp-1")
    message = str(excinfo.value)
    # The error has to name the alternatives, or it just moves the puzzle.
    assert "_query_dir" in message
    assert "_config_dir" in message
    assert "_whole_campaign_dir" in message


def test_config_dir_fetches_only_the_config_snapshot(svc):
    service, storage = svc
    config_dir = service._config_dir("camp-1")

    assert (config_dir / "campaign.vast").read_bytes() == PLOTS_VAST
    assert _downloaded(storage) == {
        "camp-1/_config/campaign.vast",
        "camp-1/_config/panel/bundle.js",
    }


def test_listing_declared_plots_does_not_fetch_the_campaign(svc):
    """The regression that motivated all of this."""
    service, storage = svc
    response = service.list_campaign_plots("camp-1")

    assert [p.title for p in response.plots] == ["speed"]
    assert not any(key.endswith("rosbag.db3") for key in _downloaded(storage))


def test_resolving_a_panel_asset_does_not_fetch_the_campaign(svc):
    service, storage = svc
    resolved = service.resolve_campaign_panel_asset("camp-1", "panel/bundle.js")

    assert resolved.endswith("panel/bundle.js")
    assert not any(key.endswith("rosbag.db3") for key in _downloaded(storage))


def test_listing_panels_reads_the_config_snapshot_not_an_unfetched_campaign(svc):
    """The one that reached a browser.

    ``list_campaign_panels`` resolved the ``.vast`` through ``_campaign_dir``, which on this
    lane names the fetched *record* cache: for a campaign this pod does not drive that holds
    ``campaign.db`` and ``_execution/`` and no ``_config/`` at all, so ``campaign_vast`` raised
    "no .vast" and the route answered 500. The run view asks for its panels before it draws
    anything, so that single failure emptied the entire view -- no transport bar, no
    backend-contributed ``scene3d``, just the run-selection header -- which reads as the 3D
    scene being broken rather than as a config read taking the wrong seam.

    Not caught by the structural guard above: ``_campaign_dir`` is not ``_data_dir``, so it
    never raised. It answered, with a directory that was simply empty.
    """
    service, storage = svc
    response = service.list_campaign_panels("camp-1")

    # The transport bar is contributed to every run view, so its absence is the empty view.
    assert "playback" in [panel["type"] for panel in response.panels]
    assert not any(key.endswith("rosbag.db3") for key in _downloaded(storage))


def test_whole_campaign_dir_is_the_explicit_way_to_ask_for_everything(monkeypatch, svc):
    """``_whole_campaign_dir`` still fetches — the point is that it says so."""
    service, _ = svc
    called = []
    monkeypatch.setattr(ClusterService, "fetch_campaign",
                        lambda self, cid, *a, **kw: called.append(cid) or "/tmp/camp-1")

    assert service._whole_campaign_dir("camp-1") == "/tmp/camp-1"
    assert called == ["camp-1"]
