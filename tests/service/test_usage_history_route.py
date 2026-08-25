# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``/usage/history`` answers about a period, and says what it can and cannot cover.

Two properties carry the whole endpoint. Capacity is recorded per sample, so a cluster that
changed size does not have its past redrawn; and ``service_started_at`` is reported, so a
short history cannot be read as an idle one -- the recording lives in the serving process,
which is the floor on what it can ever say.
"""

import time

import pytest

from robovast.service.app import build_app
from robovast.service.interface import Routes, UsageSample
from robovast.service.local_transport import LocalTransport
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore


@pytest.fixture
def client(tmp_path):
    from starlette.testclient import TestClient
    store = WorkspaceStore(registry=WorkspaceRegistry(root=tmp_path / "workspaces"))
    app = build_app(LocalTransport(store=store), mount_mcp=False, auth_token="t")
    return TestClient(app, headers={"Authorization": "Bearer t"})


def _fill(app, samples):
    """Put readings in *this app's* ring.

    Per-app rather than a module global, so two apps built in one process record
    separately -- which is why it is reached through ``app.state`` and not imported.
    """
    app.state.usage_ring.clear()
    app.state.usage_ring.extend(samples)


def test_an_empty_recording_is_an_empty_list_not_an_error(client):
    body = client.get(Routes.USAGE_HISTORY).json()
    assert body["samples"] == []
    assert body["sample_interval_s"] > 0
    assert body["service_started_at"] > 0, (
        "the floor on what this can cover has to be reported even with nothing recorded")


def test_capacity_is_per_sample_so_a_resized_cluster_keeps_its_past(client):
    """A node joining or draining changes the denominator.

    Held against a scalar capacity, last night's readings would be redrawn against today's
    size -- a cluster that doubled would show yesterday at half the load it ran at.
    """
    now = time.time()
    _fill(client.app, [
        UsageSample(at=now - 60, cpu_used=8, cpu_capacity=16,
                    memory_used_bytes=1, memory_capacity_bytes=2),
        UsageSample(at=now - 30, cpu_used=8, cpu_capacity=64,
                    memory_used_bytes=1, memory_capacity_bytes=8),
    ])
    got = client.get(Routes.USAGE_HISTORY).json()["samples"]
    assert [s["cpu_capacity"] for s in got] == [16, 64]


def test_a_window_only_returns_what_falls_inside_it(client):
    now = time.time()
    _fill(client.app, [
        UsageSample(at=now - 7200, cpu_used=1, cpu_capacity=4,
                    memory_used_bytes=1, memory_capacity_bytes=2),
        UsageSample(at=now - 60, cpu_used=2, cpu_capacity=4,
                    memory_used_bytes=1, memory_capacity_bytes=2),
    ])
    assert len(client.get(f"{Routes.USAGE_HISTORY}?window=1h").json()["samples"]) == 1
    assert len(client.get(f"{Routes.USAGE_HISTORY}?window=24h").json()["samples"]) == 2


def test_a_long_window_is_strided_rather_than_truncated(client):
    """Coarser over the whole span, never the recent slice of it.

    Truncating would answer a different question from the one asked -- "the last 24 hours"
    would quietly become "the last hour, in detail".
    """
    now = time.time()
    _fill(client.app, [
        UsageSample(at=now - i, cpu_used=1, cpu_capacity=4,
                    memory_used_bytes=1, memory_capacity_bytes=2)
        for i in range(2880, 0, -1)])
    body = client.get(f"{Routes.USAGE_HISTORY}?window=24h").json()
    assert len(body["samples"]) <= 400, "a reply must not carry thousands of points"
    assert body["step_s"] > body["sample_interval_s"], "striding must be reported"
    span = body["samples"][-1]["at"] - body["samples"][0]["at"]
    assert span > 2000, "the reply lost the span it was asked about"


def test_an_unknown_window_is_refused(client):
    """``Literal`` on the parameter: an unrecognised window is an error, not a default.

    Silently answering a different period than the caller asked for is the failure mode
    worth refusing -- they would plot it and believe it.
    """
    assert client.get(f"{Routes.USAGE_HISTORY}?window=7d").status_code == 422
