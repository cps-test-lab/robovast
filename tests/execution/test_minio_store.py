# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The store's volume, and the refusals that guard changing it.

The store holds every finished campaign, so what backs it and what recreating its pod costs
are the two facts an operator has to be told correctly.
"""

import io
from unittest.mock import MagicMock

import pytest
import yaml

from robovast.execution.cluster_config import minio_store, rke2


def _pod(store="emptyDir", detail=None):
    """A live store pod as the API returns one, backed the way the caller asks."""
    mount = MagicMock(name="minio-storage", mount_path=minio_store.MINIO_DATA_DIR)
    mount.name = minio_store.MINIO_VOLUME_NAME
    container = MagicMock(volume_mounts=[mount])
    container.name = minio_store.MINIO_CONTAINER_NAME
    volume = MagicMock(host_path=None, persistent_volume_claim=None, empty_dir=None)
    volume.name = minio_store.MINIO_VOLUME_NAME
    if store == "emptyDir":
        volume.empty_dir = object()
    elif store == "hostPath":
        volume.host_path = MagicMock(path=detail)
    elif store == "claim":
        volume.persistent_volume_claim = MagicMock(claim_name=detail)
    pod = MagicMock()
    pod.spec.containers = [container]
    pod.spec.volumes = [volume]
    return pod


@pytest.fixture
def live(monkeypatch):
    """Install a live store pod for the refusal to inspect."""
    def _install(pod):
        api = MagicMock()
        api.read_namespaced_pod.return_value = pod
        monkeypatch.setattr(minio_store.client, "CoreV1Api", lambda *a, **k: api)
    return _install


def test_the_store_is_a_hostpath_by_default():
    """An emptyDir empties on restart, and the store holds the only complete copy."""
    volume = minio_store.store_volume("/var/lib/robovast-store")

    assert volume["hostPath"]["path"] == "/var/lib/robovast-store"
    assert volume["hostPath"]["type"] == "DirectoryOrCreate"
    assert "emptyDir" not in volume


def test_a_class_backs_the_store_with_a_claim():
    volume = minio_store.store_volume("/ignored", "local-path")

    assert volume["persistentVolumeClaim"]["claimName"] == minio_store.MINIO_VOLUME_NAME
    assert minio_store.store_pvc_manifest("default", "local-path", "1Ti")["spec"][
        "resources"]["requests"]["storage"] == "1Ti"
    assert minio_store.store_pvc_manifest("default", "") is None


def test_the_volume_is_placed_through_the_container_that_mounts_it():
    """Found by the mount, not by the volume's name, so a provider cannot be missed."""
    docs = list(yaml.safe_load_all(io.StringIO(rke2.MINIO_MANIFEST_RKE2)))
    out = minio_store.apply_store_volume(
        docs, minio_store.store_volume("/media/data/store"))

    pod = next(d for d in out if d["kind"] == "Pod")
    volume = next(v for v in pod["spec"]["volumes"]
                  if v["name"] == minio_store.MINIO_VOLUME_NAME)
    assert volume["hostPath"]["path"] == "/media/data/store"
    assert "emptyDir" not in volume


def test_a_claim_is_ordered_before_the_pod_that_mounts_it():
    """A pod scheduled against a claim that does not exist yet stays Pending."""
    docs = list(yaml.safe_load_all(io.StringIO(rke2.MINIO_MANIFEST_RKE2)))
    out = minio_store.apply_store_volume(
        docs, minio_store.store_volume("", "local-path"),
        minio_store.store_pvc_manifest("default", "local-path"))

    assert out[0]["kind"] == "PersistentVolumeClaim"


def test_an_unchanged_backing_is_not_refused(live):
    live(_pod("hostPath", "/media/data/store"))
    minio_store.refuse_a_store_the_manifest_cannot_change(
        "default", minio_store.store_volume("/media/data/store"))


def test_converting_an_ephemeral_store_says_the_campaigns_go(live):
    """The one case where recreating the pod destroys data, and it must say so."""
    live(_pod("emptyDir"))
    with pytest.raises(RuntimeError) as excinfo:
        minio_store.refuse_a_store_the_manifest_cannot_change(
            "default", minio_store.store_volume("/media/data/store"))

    message = str(excinfo.value)
    assert "DISCARDS" in message
    assert "vast share" in message


def test_moving_a_durable_store_does_not_claim_data_loss(live):
    """A blanket warning here would be false, and one that is false is one nobody reads."""
    live(_pod("hostPath", "/var/lib/robovast-store"))
    with pytest.raises(RuntimeError) as excinfo:
        minio_store.refuse_a_store_the_manifest_cannot_change(
            "default", minio_store.store_volume("/media/data/store"))

    message = str(excinfo.value)
    assert "DISCARDS" not in message
    assert "/var/lib/robovast-store" in message, "the operator must be told where the bytes are"
    assert "--store-path /var/lib/robovast-store" in message, "and how to leave them there"


def test_a_store_placement_is_refused_where_the_store_is_a_bucket():
    with pytest.raises(RuntimeError) as excinfo:
        minio_store.refuse_a_store_placement("gcp", "/media/data/store", "", "gcs")

    assert "--store-path" in str(excinfo.value)
    assert "bucket" in str(excinfo.value)


def test_a_store_placement_is_refused_where_the_provider_builds_its_own_volume():
    with pytest.raises(RuntimeError) as excinfo:
        minio_store.refuse_a_store_placement("azure", "", "managed-csi", "s3")

    assert "--store-class" in str(excinfo.value)


def test_no_placement_is_never_refused():
    minio_store.refuse_a_store_placement("gcp", "", "", "gcs")


def test_a_store_the_kubelet_cannot_measure_says_so_rather_than_reading_empty():
    """The pod is in the stats and its volume is not -- what a hostPath looks like from here.

    Reported as a reason rather than as "keep looking", because no further node will answer
    and a caller walking them would spend its budget learning that again.
    """
    summaries = {"n1": {"pods": [{"podRef": {"name": minio_store.MINIO_POD_NAME},
                                  "volume": []}]}}
    used, ceiling, reason = minio_store.store_usage(summaries)

    assert (used, ceiling) == (None, None)
    assert reason == minio_store.HOSTPATH_NOT_MEASURED


def test_a_store_on_a_node_not_yet_read_says_keep_looking():
    used, ceiling, reason = minio_store.store_usage({"n1": {"pods": []}})

    assert (used, ceiling, reason) == (None, None, "")


def test_the_ceiling_is_what_the_filesystem_will_still_take():
    """Not ``capacityBytes``: a volume with no size limit reports the whole node filesystem,
    which it shares with the images, the containers and every campaign directory."""
    summaries = {"n1": {"pods": [{
        "podRef": {"name": minio_store.MINIO_POD_NAME},
        "volume": [{"name": minio_store.MINIO_VOLUME_NAME, "usedBytes": 200,
                    "availableBytes": 600, "capacityBytes": 100_000}]}]}}
    assert minio_store.store_usage(summaries) == (200, 800, "")
