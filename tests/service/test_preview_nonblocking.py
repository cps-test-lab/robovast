# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``preview_configurations(wait=False)`` answers at once and composes in the background.

The launcher polls it for the filter's dropdown, so a call never waits on composition: it
starts one, reports it as ``composing``, and serves the landed preview until the ``.vast``
changes.
"""

import contextlib
import os
import time
from types import SimpleNamespace

import pytest

from robovast.execution.cluster_execution.cluster_service import ClusterService

_SCENARIO = """\
import osc.robotics

scenario nav:
    speed: length = 1.0m
    do serial:
        wait elapsed(1s)
"""

_VAST = """\
version: 6
metadata: {name: names-test}
configuration:
%s
execution:
  containers:
    scenario: {image: 'family:robovast'}
  runs: 1
  scenario_file: scenario.osc
"""


def _impl(tmp_path, blocks):
    (tmp_path / "scenario.osc").write_text(_SCENARIO)
    vast = tmp_path / "campaign.vast"
    vast.write_text(_VAST % "\n".join(f"- name: {b}" for b in blocks))
    impl = ClusterService(namespace="ns", cluster_config_name="x",
                          cluster_config_kwargs={}, reap_on_start=False)
    # pylint: disable=protected-access
    impl._resolve_project = lambda workspace_id, path: SimpleNamespace(config_path=str(vast))
    # Composing these needs no helper container, so no cluster to reach one through.
    impl._aux_runner_context = lambda *a, **kw: contextlib.nullcontext()
    return impl, vast


def _settled(impl):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        result = impl.preview_configurations("ws", wait=False)
        if result.state != "composing":
            return result
        time.sleep(0.05)
    raise AssertionError("composition never landed")


def test_the_first_call_does_not_wait(tmp_path):
    impl, _ = _impl(tmp_path, ["cell0"])
    assert impl.preview_configurations("ws", wait=False).state == "composing"
    assert [c.name for c in _settled(impl).configurations] == ["cell0"]


def test_an_edited_vast_is_composed_again(tmp_path):
    impl, vast = _impl(tmp_path, ["cell0"])
    _settled(impl)
    vast.write_text(_VAST % "- name: cell0\n- name: cell1")
    stat = vast.stat()
    os.utime(vast, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    assert impl.preview_configurations("ws", wait=False).state == "composing"
    assert [c.name for c in _settled(impl).configurations] == ["cell0", "cell1"]


def test_a_failed_composition_says_why(tmp_path):
    impl, vast = _impl(tmp_path, ["cell0"])
    vast.write_text(_VAST % "- name: cell0\n  variations:\n  - NoSuchVariation: {}")
    result = _settled(impl)
    assert result.state == "failed" and result.error and not result.configurations


def test_a_search_vast_is_refused(tmp_path):
    impl, vast = _impl(tmp_path, ["cell0"])
    vast.write_text(vast.read_text() + "search: {}\n")
    with pytest.raises(ValueError, match="search"):
        impl.preview_configurations("ws", wait=False)
