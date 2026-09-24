# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The core has to work when the cluster package is not installed.

Every core→cluster import is deferred and inside a ``try``, which is not sufficient on its
own: what matters is whether the import is reached at all on a path that has nothing to do
with a cluster, and that the ``except`` then says something true.

These simulate a missing cluster package with an import hook rather than by uninstalling
anything. The hook matches dotted names, so ``robovast.execution.cluster_execution`` is
blocked too, not only top-level packages.
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

    # None is how a MetaPathFinder declines a module
    def find_spec(self, name, path=None, target=None):  # pylint: disable=useless-return
        if any(name == n or name.startswith(n + ".") for n in self.names):
            raise ImportError(f"{name}: cluster package not installed")
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
    """`_record_controller_outcome` uploads control-plane artifacts only for a backend
    with a cluster config; without one and without the cluster package, it warns about no
    upload that was never going to happen.
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


def test_doctor_reports_a_missing_cluster_package_instead_of_raising(without):
    """`vast doctor` exists to say what is wrong. Dying while finding out is the one
    failure it cannot have, so a missing `load_kube_config` is a reported Check too."""
    without("robovast.execution.cluster_execution.kube_client")
    from robovast.client.doctor import check_cluster

    checks = check_cluster()
    assert [c.name for c in checks] == ["cluster support"]
    assert checks[0].status == "warn", "a client install is not broken for lacking it"
    assert "not installed" in checks[0].detail


def test_the_whole_doctor_still_runs_without_the_cluster_package(without):
    without("robovast.execution.cluster_execution.kube_client")
    from robovast.client.doctor import run_checks

    assert run_checks(), "doctor produced no checks at all"
