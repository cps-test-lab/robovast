# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for storage-client selection in ``in_pod_storage``.

The in-process driver builds its storage client through ``storage_client_for``.
Off-cluster it must reach a *host-reachable* endpoint, and it must do so
uniformly across providers: embedded MinIO (S3 + port-forward), external S3
(direct), and native GCS (own client, no S3 endpoint at all).
"""

from robovast.execution.cluster_config.base_config import BaseConfig
from robovast.execution.cluster_execution import in_pod_storage


class _S3Config(BaseConfig):
    """Minimal S3-backed config (embedded MinIO by default)."""

    def get_s3_endpoint(self):
        return "http://robovast:9000"

    def get_s3_credentials(self):
        return ("ak", "sk")


class _GcsConfig(BaseConfig):
    """Minimal native-GCS config."""

    def get_storage_backend(self):
        return "gcs"

    def get_gcs_key_json(self):
        return '{"type": "service_account"}'


def test_s3_client_uses_the_driver_endpoint_override(monkeypatch):
    """The driver's S3 client resolves its endpoint through ``get_driver_s3_endpoint``.

    The client is handed a *lazy* resolver (not a baked-in endpoint), so an
    off-cluster host redirecting to a port-forward is honoured, while the
    cluster-internal ``get_s3_endpoint`` stays reserved for job manifests.
    """
    seen = {}
    monkeypatch.setattr(in_pod_storage, "_S3StorageClient",
                        lambda **kw: seen.update(kw) or "s3-client")

    cfg = _S3Config()
    cfg.set_driver_s3_endpoint_resolver(
        lambda force_reconnect=False, current=None: "http://localhost:18099")
    assert in_pod_storage.storage_client_for(cfg) == "s3-client"
    # Invoking the resolver the client was given yields the host-reachable override.
    assert seen["endpoint_resolver"]() == "http://localhost:18099"


def test_s3_client_defaults_to_cluster_endpoint(monkeypatch):
    """With no resolver installed (in-cluster) the resolver returns the cluster endpoint."""
    seen = {}
    monkeypatch.setattr(in_pod_storage, "_S3StorageClient",
                        lambda **kw: seen.update(kw))
    in_pod_storage.storage_client_for(_S3Config())
    assert seen["endpoint_resolver"]() == "http://robovast:9000"


def test_gcs_client_ignores_the_s3_endpoint_path(monkeypatch):
    """Native GCS routes to the GCS client and never touches the S3 endpoint.

    GCS is reachable from anywhere, so it needs neither a driver endpoint nor a
    port-forward — installing an exploding resolver proves it is never consulted.
    """
    seen = {}
    monkeypatch.setattr(in_pod_storage, "_GcsStorageClient",
                        lambda **kw: seen.update(kw) or "gcs-client")

    cfg = _GcsConfig()
    cfg.set_driver_s3_endpoint_resolver(
        lambda: (_ for _ in ()).throw(AssertionError("S3 endpoint consulted for GCS")))
    assert in_pod_storage.storage_client_for(cfg) == "gcs-client"
    assert seen["key_json"] == '{"type": "service_account"}'


def test_resolve_driver_endpoint_embedded_opens_the_tunnel():
    """Embedded MinIO has no host route → the port-forward opener is invoked."""
    cfg = _S3Config()  # uses_embedded_s3() defaults to True
    # The opener is called with (force_reconnect, current) — see
    # ClusterService._minio_port_forward_endpoint.
    assert cfg.resolve_driver_s3_endpoint(lambda *a, **k: "http://localhost:1") \
        == "http://localhost:1"


def test_resolve_driver_endpoint_external_stays_direct():
    """A directly-reachable host endpoint never opens a tunnel."""
    class _External(_S3Config):
        def uses_embedded_s3(self):
            return False

        def get_host_s3_endpoint(self):
            return "https://s3.example.com"

    def _boom():
        raise AssertionError("port-forward opened for a directly-reachable store")

    assert _External().resolve_driver_s3_endpoint(_boom) == "https://s3.example.com"


# -- an unanswering store ----------------------------------------------------
#
# botocore reports "no answer" as a family of transport exceptions (read timeout,
# connect timeout, endpoint connection, connection closed). ``_resilient`` is the one
# place every S3 operation here passes through, so it is where they become a single
# named error — otherwise each caller has to enumerate the family, and the caller that
# did (``EndpointConnectionError`` only) still let a reset connection out as a 500.


def _interactive_client(monkeypatch, boto_client):
    """An interactive S3 client whose boto client is *boto_client*.

    Interactive means ``reconnect_attempts == 1``: the client fails on the first
    transport error instead of rebuilding itself (and with it the stub) first.
    """
    cfg = _S3Config()
    cfg.set_driver_s3_endpoint_resolver(
        lambda force_reconnect=False, current=None: "http://localhost:18080")
    client = in_pod_storage.storage_client_for(cfg, interactive=True)
    monkeypatch.setattr(client, "_s3", boto_client)
    return client


def test_a_reset_connection_names_the_endpoint_and_the_object(monkeypatch):
    """The reported failure: a ``head_object`` behind ``campaign_data_status``."""
    import pytest
    from botocore.exceptions import ConnectionClosedError

    from robovast.common.errors import ObjectStoreUnreachableError

    class _Reset:
        def head_object(self, **_kw):
            raise ConnectionClosedError(
                endpoint_url="http://localhost:18080/camp-1/_execution/data.db")

    client = _interactive_client(monkeypatch, _Reset())
    with pytest.raises(ObjectStoreUnreachableError) as excinfo:
        client.stat_object("camp-1", "_execution/data.db")

    message = str(excinfo.value)
    # The endpoint the *client* holds, not only the one botocore quotes: a rotating
    # port-forward means those differ, and only the former is worth probing.
    assert "http://localhost:18080" in message
    assert "s3://camp-1/_execution/data.db" in message
    # A self-contained sentence, so nothing downstream prints a stack for it.
    assert excinfo.value.include_traceback is False


def test_a_missing_object_is_still_not_an_error(monkeypatch):
    """The store *answered* — 404 stays the caller's `None`, not an unreachable store."""
    from botocore.exceptions import ClientError

    class _Missing:
        def head_object(self, **_kw):
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")

    client = _interactive_client(monkeypatch, _Missing())
    assert client.stat_object("camp-1", "_execution/data.db") is None


# -- upload iteration robustness --------------------------------------------

def test_iter_files_skips_broken_symlink(tmp_path):
    """A dangling ``job`` link (interrupted run) is skipped, not fatal.

    finalize_campaign's upload_dir walks the campaign root; an interrupted campaign
    leaves ``<config>/<run>/job`` pointing at an ``_jobs/...`` target that was never
    produced. os.stat on that link used to abort the whole upload.
    """
    (tmp_path / "test.xml").write_text("<ok/>")
    run = tmp_path / "cfg" / "0"
    run.mkdir(parents=True)
    (run / "result.txt").write_text("data")
    (run / "job").symlink_to(tmp_path / "_jobs" / "missing")  # dangling

    rels = {rel for _abs, rel in in_pod_storage._iter_files(str(tmp_path))}
    assert rels == {"test.xml", "cfg/0/result.txt"}  # broken link skipped, rest kept


def test_iter_files_keeps_resolvable_symlink_target(tmp_path):
    """A ``job`` link to a real dir is a directory — its files upload via the target."""
    jobs = tmp_path / "_jobs" / "job-0"
    jobs.mkdir(parents=True)
    (jobs / "sysinfo.yaml").write_text("x")
    run = tmp_path / "cfg" / "0"
    run.mkdir(parents=True)
    (run / "job").symlink_to(jobs)

    rels = {rel for _abs, rel in in_pod_storage._iter_files(str(tmp_path))}
    # The real file under _jobs is yielded; the symlink is a dir, not a bogus file.
    assert "_jobs/job-0/sysinfo.yaml" in rels
    assert "cfg/0/job" not in rels


def test_is_executable_tolerates_missing_path(tmp_path):
    assert in_pod_storage._is_executable(str(tmp_path / "gone")) is False


# -- the campaign index -----------------------------------------------------
#
# The index is what makes a campaign in the object store discoverable at all: without
# it the service can only list what happens to be on local disk, which in-pod is
# nothing from a previous service life.


class _FakeStorage:
    """In-memory stand-in for the object-store keys the index cares about."""

    def __init__(self):
        self.objects: dict[tuple, bytes] = {}

    def upload_file(self, local_path, bucket, key):
        with open(local_path, "rb") as fh:
            self.objects[(bucket, key)] = fh.read()

    def list_keys(self, bucket, prefix=""):
        head = f"{prefix.rstrip('/')}/" if prefix else ""
        return sorted(k for (b, k) in self.objects if b == bucket and k.startswith(head))

    def delete_prefix(self, bucket, prefix):
        head = in_pod_storage._delete_key_prefix(bucket, prefix)
        gone = [(b, k) for (b, k) in self.objects if b == bucket and k.startswith(head)]
        for key in gone:
            del self.objects[key]
        return len(gone)


def test_campaign_index_round_trips_the_id_verbatim():
    """A campaign id survives the index exactly — including ``_`` and upper case.

    This is the whole reason the index exists rather than a bucket listing: a
    per-campaign bucket is named ``id.lower().replace("_","-")``, so recovering the id
    from the bucket name is guesswork that silently returns a *different* id. The marker
    key carries the id verbatim, so nothing has to be undone.
    """
    storage, cfg = _FakeStorage(), _S3Config()
    campaign_id = "My_Campaign-2026-07-28-12000000"
    in_pod_storage.mark_campaign_indexed(storage, cfg, campaign_id, "2026-07-28T12:00:00+00:00")

    assert in_pod_storage.list_indexed_campaigns(storage, cfg) == [
        (campaign_id, "2026-07-28T12:00:00+00:00")]
    # And it is not the sanitised bucket name the old listing would have produced.
    assert campaign_id != campaign_id.lower().replace("_", "-")


def test_campaign_index_marker_has_no_body():
    """The key *is* the record. There is no body, hence nowhere to put a status —
    ``_execution/outcome.json`` stays the single source of truth for the phase."""
    storage, cfg = _FakeStorage(), _S3Config()
    in_pod_storage.mark_campaign_indexed(storage, cfg, "c-2026-07-28-12000000", "t")
    assert set(storage.objects.values()) == {b""}


def test_campaign_index_marking_is_idempotent():
    storage, cfg = _FakeStorage(), _S3Config()
    for _ in range(3):
        in_pod_storage.mark_campaign_indexed(storage, cfg, "c-2026-07-28-12000000", "t")
    assert len(in_pod_storage.list_indexed_campaigns(storage, cfg)) == 1


def test_unmark_removes_every_marker_for_that_campaign():
    """Deleting a campaign must not leave a marker that resurrects it in the next
    listing — including a stale one from an earlier launch of the same id."""
    storage, cfg = _FakeStorage(), _S3Config()
    in_pod_storage.mark_campaign_indexed(storage, cfg, "c-2026-07-28-12000000", "t1")
    in_pod_storage.mark_campaign_indexed(storage, cfg, "c-2026-07-28-12000000", "t2")
    in_pod_storage.mark_campaign_indexed(storage, cfg, "other-2026-07-28-12000000", "t1")

    in_pod_storage.unmark_campaign_indexed(storage, cfg, "c-2026-07-28-12000000")
    assert [cid for cid, _ in in_pod_storage.list_indexed_campaigns(storage, cfg)] == [
        "other-2026-07-28-12000000"]


def test_unmark_refuses_an_empty_campaign_id():
    """An empty id would delete the whole index — every campaign at once."""
    import pytest
    with pytest.raises(ValueError):
        in_pod_storage.unmark_campaign_indexed(_FakeStorage(), _S3Config(), "")


def test_malformed_index_key_is_skipped_not_guessed():
    """One unparseable marker costs one campaign's listing, not the whole listing."""
    storage, cfg = _FakeStorage(), _S3Config()
    in_pod_storage.mark_campaign_indexed(storage, cfg, "good-2026-07-28-12000000", "t")
    bucket = in_pod_storage.campaign_index_bucket(cfg)
    storage.objects[(bucket, f"{in_pod_storage.CAMPAIGN_INDEX_PREFIX}/nostamp")] = b""

    assert [cid for cid, _ in in_pod_storage.list_indexed_campaigns(storage, cfg)] == [
        "good-2026-07-28-12000000"]


def test_index_bucket_prefers_the_deployments_shared_bucket():
    """With a shared bucket the index is a prefix in it; nothing extra is created."""
    class _Shared(_S3Config):
        def get_s3_bucket(self):
            return "team-bucket"

    assert in_pod_storage.campaign_index_bucket(_Shared()) == "team-bucket"


def test_index_bucket_is_our_own_when_campaigns_get_their_own_buckets():
    """Per-campaign-bucket mode: the index belongs to no campaign, so it gets a bucket
    of its own — creatable because the S3 namespace is the deployment's own endpoint."""
    assert in_pod_storage.campaign_index_bucket(_S3Config()) \
        == in_pod_storage.CAMPAIGN_INDEX_BUCKET


def test_index_bucket_on_gcs_without_a_bucket_is_reported_not_invented():
    """A GCS bucket name is global to all of Google Cloud and that client creates no
    buckets, so guessing one would collide with a stranger's bucket or 403."""
    import pytest
    with pytest.raises(ValueError, match="needs a bucket"):
        in_pod_storage.campaign_index_bucket(_GcsConfig())
