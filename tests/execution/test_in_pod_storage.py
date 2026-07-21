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
