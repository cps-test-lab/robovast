# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for host-side variation-plugin detection (discover_plugin_installs).

Composition of scenario variations runs inside the controller pod, so any
third-party ``robovast.variation_types`` plugin installed on the host must be
shipped into the pod. These tests cover the three PEP 610 provenance branches
(local dir, VCS, index), built-in exclusion, name canonicalization and the
raise-on-build-failure contract, with the wheel build and entry-point lookup
monkeypatched so nothing is actually built or installed.
"""

# pylint: disable=import-outside-toplevel

import json

import pytest

from robovast.execution.cluster_execution import controller_launcher as cl


class _FakeDist:
    """Minimal importlib.metadata.Distribution stand-in.

    ``direct_url`` is the parsed PEP 610 ``direct_url.json`` (or ``None`` for an
    index install); ``read_text`` serves it back as the launcher expects.
    """

    def __init__(self, name, version, direct_url=None):
        self.name = name
        self.version = version
        self._direct_url = direct_url

    def read_text(self, filename):
        if filename == "direct_url.json" and self._direct_url is not None:
            return json.dumps(self._direct_url)
        return None


class _FakeEP:
    def __init__(self, name, dist):
        self.name = name
        self.dist = dist


def _patch_entry_points(monkeypatch, eps):
    # discover_plugin_installs does a local ``from importlib.metadata import
    # entry_points``, which resolves the attribute at call time, so patch it there.
    import importlib.metadata
    monkeypatch.setattr(importlib.metadata, "entry_points", lambda *a, **k: eps)


def _patch_pip_wheel(monkeypatch, built):
    """Record (source_dir, label) calls; return a fake wheel path per call."""
    def fake(source_dir, label):
        built.append((source_dir, label))
        return f"/tmp/fake_wheel/{label}-0-py3-none-any.whl"
    monkeypatch.setattr(cl, "_pip_wheel", fake)


def test_local_dir_plugin_builds_wheel(monkeypatch):
    dist = _FakeDist("scenario_mt", "0.1.0",
                     {"dir_info": {"editable": True},
                      "url": "file:///home/fred/git/metamorphic_testing"})
    _patch_entry_points(monkeypatch, [_FakeEP("AddGoal", dist),
                                      _FakeEP("RemoveGoal", dist)])
    built = []
    _patch_pip_wheel(monkeypatch, built)

    wheels, specs = cl.discover_plugin_installs()

    # One dist despite two entry points; built once from the local source dir.
    assert built == [("/home/fred/git/metamorphic_testing", "scenario_mt")]
    assert wheels == ["/tmp/fake_wheel/scenario_mt-0-py3-none-any.whl"]
    assert specs == []


def test_vcs_plugin_passes_through_direct_url(monkeypatch):
    dist = _FakeDist("weird_plugin", "2.0.0",
                     {"vcs_info": {"vcs": "git", "commit_id": "abc123"},
                      "url": "https://github.com/acme/weird_plugin"})
    _patch_entry_points(monkeypatch, [_FakeEP("Weird", dist)])
    _patch_pip_wheel(monkeypatch, [])

    wheels, specs = cl.discover_plugin_installs()

    assert wheels == []
    assert specs == ["weird_plugin @ git+https://github.com/acme/weird_plugin@abc123"]


def test_index_plugin_pins_version(monkeypatch):
    dist = _FakeDist("published_plugin", "3.4.5", direct_url=None)
    _patch_entry_points(monkeypatch, [_FakeEP("Pub", dist)])
    _patch_pip_wheel(monkeypatch, [])

    wheels, specs = cl.discover_plugin_installs()

    assert wheels == []
    assert specs == ["published_plugin==3.4.5"]


def test_builtins_are_excluded(monkeypatch):
    robovast = _FakeDist("robovast", "1.0.0", None)
    # robovast-nav is installed editable from a local dir but must still be skipped
    # (build_dev_wheels already ships it); canonicalization handles the underscore.
    nav = _FakeDist("robovast_nav", "1.0.0",
                    {"dir_info": {}, "url": "file:///repo/src/robovast_nav"})
    _patch_entry_points(monkeypatch, [_FakeEP("ParameterVariationList", robovast),
                                      _FakeEP("FloorplanVariation", nav)])
    built = []
    _patch_pip_wheel(monkeypatch, built)

    wheels, specs = cl.discover_plugin_installs()

    assert wheels == []
    assert specs == []
    assert built == []


def test_build_failure_raises(monkeypatch):
    dist = _FakeDist("scenario_mt", "0.1.0",
                     {"dir_info": {}, "url": "file:///src/scenario_mt"})
    _patch_entry_points(monkeypatch, [_FakeEP("AddGoal", dist)])
    monkeypatch.setattr(cl, "_pip_wheel", lambda source_dir, label: None)

    with pytest.raises(RuntimeError, match="scenario_mt"):
        cl.discover_plugin_installs()


def test_skip_env_opts_out(monkeypatch):
    dist = _FakeDist("scenario_mt", "0.1.0",
                     {"dir_info": {}, "url": "file:///src/scenario_mt"})
    _patch_entry_points(monkeypatch, [_FakeEP("AddGoal", dist)])
    built = []
    _patch_pip_wheel(monkeypatch, built)
    monkeypatch.setenv(cl._SKIP_PLUGIN_INJECTION_ENV, "1")

    wheels, specs = cl.discover_plugin_installs()

    assert (wheels, specs) == ([], [])
    assert built == []


def test_canonical_dist_name():
    assert cl._canonical_dist_name("Scenario_MT") == "scenario-mt"
    assert cl._canonical_dist_name("robovast.nav") == "robovast-nav"
    assert cl._canonical_dist_name("robovast-nav") == "robovast-nav"
