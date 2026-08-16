# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The core has to work when the cluster lane is not installed.

Every remaining core→cluster import is deferred and inside a ``try``, which looks
sufficient and is not: what matters is whether the import is reached at all on a path
that has nothing to do with a cluster, and what the ``except`` then says. Both failures
here were of that shape — the code degraded, but degraded into a *lie*.

These simulate a missing cluster package with an import hook rather than by uninstalling
anything. Note the hook has to match dotted names: an earlier version of this test only
matched top-level packages, so ``robovast.execution.cluster_execution`` sailed through
and the tests passed against code that would have failed.
"""

import logging
import os
import sys
import tempfile
from unittest.mock import patch

import pytest


class _MissingPackage:
    """Make a dotted module (and everything under it) un-importable."""

    def __init__(self, *names):
        self.names = names

    def find_spec(self, name, path=None, target=None):
        if any(name == n or name.startswith(n + ".") for n in self.names):
            raise ImportError(f"{name}: cluster lane not installed")
        return None


@pytest.fixture
def without(monkeypatch):
    def _install(*names):
        finder = _MissingPackage(*names)
        sys.meta_path.insert(0, finder)
        monkeypatch.setattr(sys, "meta_path", sys.meta_path)
        for loaded in [m for m in sys.modules if any(m.startswith(n) for n in names)]:
            monkeypatch.delitem(sys.modules, loaded, raising=False)
        yield_finder = finder
        return yield_finder
    installed = []

    def _wrapped(*names):
        installed.append(_install(*names))
    try:
        yield _wrapped
    finally:
        for f in installed:
            if f in sys.meta_path:
                sys.meta_path.remove(f)


@pytest.fixture
def warnings_from():
    def _capture(logger_name):
        messages = []

        class _Capture(logging.Handler):
            def emit(self, record):
                messages.append(record.getMessage())

        handler = _Capture()
        logging.getLogger(logger_name).addHandler(handler)
        return messages, handler
    return _capture


def test_a_local_teardown_does_not_claim_a_failed_upload(without, warnings_from):
    """`_record_controller_outcome` uploads control-plane artifacts so a stateless
    service can explain a campaign after its pod is gone — a cluster-lane concern,
    correctly guarded by `cluster_config is None`. The guard used to sit *after* the
    import, so with no cluster package a purely local run logged "Could not upload
    outcome record": a warning about work that was never going to happen.
    """
    without("robovast.execution.cluster_execution")
    messages, handler = warnings_from("robovast.execution.controller")
    try:
        from robovast.common import campaign_data
        from robovast.execution import controller

        class LocalBackend:
            cluster_config = None

        class State:
            def snapshot(self):
                return {}

        root = tempfile.mkdtemp()
        os.makedirs(os.path.join(root, "_execution"), exist_ok=True)
        with patch.object(campaign_data, "write_execution_outcome", lambda *a, **k: None):
            controller._record_controller_outcome(  # noqa: SLF001
                root, "camp-local", State(), LocalBackend())

        assert not [m for m in messages if "upload" in m.lower()], messages
    finally:
        logging.getLogger("robovast.execution.controller").removeHandler(handler)


def test_doctor_reports_a_missing_cluster_lane_instead_of_raising(without):
    """`vast doctor` exists to say what is wrong. Dying while finding out is the one
    failure it cannot have — and its `load_kube_config` import sat outside the `try`
    that turns every other cluster problem into a reported Check."""
    without("robovast.execution.cluster_execution.kube_client")
    from robovast.common.cli.doctor import check_cluster

    checks = check_cluster()
    assert [c.name for c in checks] == ["cluster support"]
    assert checks[0].status == "warn", "a client install is not broken for lacking it"
    assert "not installed" in checks[0].detail


def test_the_whole_doctor_still_runs_without_the_cluster_lane(without):
    without("robovast.execution.cluster_execution.kube_client")
    from robovast.common.cli.doctor import run_checks

    assert run_checks(), "doctor produced no checks at all"
