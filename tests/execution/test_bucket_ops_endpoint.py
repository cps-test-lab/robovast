# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Embedded MinIO is reached by port-forward only from OFF the cluster.

Found against a real deployment. ``_s3_connection`` opened a ``kubectl port-forward``
whenever the cluster uses embedded MinIO, without asking where it was running. In the
service pod there is no ``kubectl`` -- and none is needed, since the cluster-internal
endpoint resolves directly -- so the first thing every campaign deletion did was raise
``FileNotFoundError: 'kubectl'``, which reached users as a bare 500 from the web UI, the
CLI and the MCP tool alike. Nothing was deleted, and no client could say why.

Both directions matter. Removing the port-forward outright would break every off-cluster
caller, which is the case it was written for.
"""


import pytest

from robovast.execution.cluster_execution import bucket_ops


class _Embedded:
    """A cluster config using embedded MinIO, with the two endpoints distinguishable."""

    def uses_embedded_s3(self):
        return True

    def get_s3_credentials(self):
        return ("key", "secret")

    def get_s3_region(self):
        return "us-east-1"

    def get_host_s3_endpoint(self):
        return "http://host.invalid:9000"

    def get_s3_endpoint(self):
        return "http://minio.default.svc.cluster.local:9000"


def test_in_pod_uses_the_cluster_endpoint_and_never_shells_out(monkeypatch):
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.43.0.1")
    monkeypatch.setattr(bucket_ops, "open_minio_port_forward",
                        lambda *a, **k: pytest.fail(
                            "in-pod must not shell out to kubectl; the cluster endpoint "
                            "resolves directly and the image has no kubectl"))
    seen = {}
    monkeypatch.setattr(bucket_ops.boto3, "client",
                        lambda *a, **k: seen.update(k) or object())

    with bucket_ops._s3_connection(_Embedded(), "default", None):
        pass

    assert seen["endpoint_url"] == "http://minio.default.svc.cluster.local:9000"


def test_off_cluster_still_opens_a_port_forward(monkeypatch):
    """The case the port-forward exists for: a host that cannot resolve cluster DNS."""
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    closed = []

    class _Proc:
        def terminate(self):
            closed.append(True)

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(bucket_ops, "open_minio_port_forward",
                        lambda ns, ctx: (_Proc(), 34567))
    seen = {}
    monkeypatch.setattr(bucket_ops.boto3, "client",
                        lambda *a, **k: seen.update(k) or object())

    with bucket_ops._s3_connection(_Embedded(), "default", None):
        pass

    assert seen["endpoint_url"] == "http://localhost:34567"
    assert closed, "the port-forward must be torn down on exit"
