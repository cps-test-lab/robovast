# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``vast service resources`` -- the terminal view of :class:`ResourceUsage`.

The verb renders every field it reads from the model the service answers with; a field
read by a name the model does not have raises after the first lines are already printed,
so the verb looks like it worked right up to the traceback.
"""

from contextlib import contextmanager

from click.testing import CliRunner

from robovast.client import service_cli
from robovast.service.interface import DiskSpace, ResourceUsage

GB = 1000 ** 3


class _Service:
    def __init__(self, usage):
        self._usage = usage

    def resource_usage(self):
        return self._usage


def _run(monkeypatch, usage):
    @contextmanager
    def fake_client(namespace, context):
        yield _Service(usage), "a test service"

    monkeypatch.setattr(service_cli, "service_client", fake_client)
    monkeypatch.setattr(service_cli, "_echo_target", lambda label: None)
    return CliRunner().invoke(service_cli.service, ["resources"], catch_exceptions=False)


def _usage(**fields):
    return ResourceUsage(backend="kubernetes", cpu_capacity=28.0, cpu_used=1.7,
                         memory_capacity_bytes=64 * 1024 ** 3,
                         memory_used_bytes=4 * 1024 ** 3, parallel_runs=True, **fields)


def test_the_verb_renders_both_volumes_the_model_carries(monkeypatch):
    result = _run(monkeypatch, _usage(
        disk=DiskSpace(capacity_bytes=600 * GB, used_bytes=100 * GB),
        results=DiskSpace(capacity_bytes=2000 * GB, used_bytes=1500 * GB)))

    assert result.exit_code == 0, result.output
    assert "  disk      500 GB free of 600 GB" in result.output
    assert "  results   500 GB free of 2000 GB" in result.output


def test_a_backend_without_a_disk_reading_says_why(monkeypatch):
    result = _run(monkeypatch, _usage(disk_unavailable="no kubelet summary"))

    assert result.exit_code == 0, result.output
    assert "disk      not read: no kubelet summary" in result.output
    assert "results" not in result.output
