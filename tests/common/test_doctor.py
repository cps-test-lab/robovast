# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``vast doctor`` — each check exists because without it the fault surfaces badly.

The properties worth pinning are about the *message*, not the verdict: every failure
carries its remedy, and an optional dependency does not fail the run. The checks other
distributions need arrive through the ``robovast.doctor_checks`` entry-point group, and a
plugin that fails is reported by name rather than crashing the command.
"""

from importlib.metadata import EntryPoint

import pytest

from robovast.client import doctor
from robovast.client.doctor import Check, DoctorOptions


def test_a_current_python_passes():
    check = doctor.check_python()
    assert check.ok and check.status == "ok"


def test_an_old_python_says_how_to_fix_it(monkeypatch):
    monkeypatch.setattr(doctor.sys, "version_info", (3, 10, 0, "final", 0))
    check = doctor.check_python()
    assert not check.ok
    assert "make venv" in check.fix


def test_docker_is_optional_so_the_cluster_path_does_not_demand_it(monkeypatch):
    from robovast.common.cli.doctor import doctor_checks

    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    [docker] = doctor_checks(DoctorOptions())
    assert docker.name == "docker"
    assert not docker.ok and docker.optional and docker.status == "warn"
    assert docker.fix


# -- the plugin mechanism ----------------------------------------------------------------

def _good(options):
    return [Check("thing", True, options.namespace)]


def _raises(_options):
    raise RuntimeError("kaboom")


def _not_checks(_options):
    return ["not a Check"]


def _plugins(monkeypatch, *names_and_targets, installed=()):
    eps = [EntryPoint(name, f"{__name__}:{target}", doctor.CHECK_GROUP)
           for name, target in names_and_targets]
    monkeypatch.setattr(doctor, "entry_points",
                        lambda group: eps if group == doctor.CHECK_GROUP else [])
    monkeypatch.setattr(doctor, "_installed", lambda name: name in installed)


def test_a_plugin_receives_the_options(monkeypatch):
    _plugins(monkeypatch, ("good", "_good"))
    checks, faults = doctor.plugin_checks(DoctorOptions(namespace="ns"))
    assert [(c.name, c.detail) for c in checks] == [("thing", "ns")]
    assert not faults


@pytest.mark.parametrize("target", ["_raises", "_not_checks", "_missing"])
def test_a_failing_plugin_is_a_failed_check_naming_it(monkeypatch, target):
    """Raising, returning the wrong thing, or not loading at all: each is reported."""
    _plugins(monkeypatch, ("broken", target), ("good", "_good"))
    checks, faults = doctor.plugin_checks(DoctorOptions())
    assert [c.name for c in checks] == ["thing"], "one bad plugin must not hide the others"
    [fault] = faults
    assert fault.name == "broken checks"
    assert not fault.ok and not fault.optional
    assert "broken" in fault.fix and fault.fix


def test_a_failing_plugin_stays_fatal_for_a_working_client(monkeypatch):
    """A broken install is not an advisory prerequisite."""
    monkeypatch.setattr(doctor, "check_client", lambda: [Check("login", True, "u")])
    _plugins(monkeypatch, ("broken", "_raises"), installed=("robovast-cluster",))
    fault = next(c for c in doctor.run_checks() if c.name == "broken checks")
    assert fault.status == "FAIL"


def test_an_installed_provider_that_registered_nothing_is_stale(monkeypatch):
    """Entry points live in installed metadata; a pyproject edit without a reinstall must
    not read as "nothing to check"."""
    _plugins(monkeypatch, installed=("robovast",))
    _checks, faults = doctor.plugin_checks(DoctorOptions())
    [fault] = faults
    assert fault.name == "robovast checks" and "stale" in fault.fix


def test_no_plugins_and_nothing_installed_is_just_the_client(monkeypatch):
    _plugins(monkeypatch)
    assert doctor.plugin_checks(DoctorOptions()) == ([], [])


def test_missing_cluster_support_is_one_advisory_line(monkeypatch):
    monkeypatch.setattr(doctor, "_installed", lambda name: False)
    [line] = doctor.check_cluster_support()
    assert line.name == "cluster support" and line.detail == "not installed"
    assert line.status == "warn" and line.fix


def test_installed_cluster_support_leaves_the_rows_to_its_plugin(monkeypatch):
    monkeypatch.setattr(doctor, "_installed", lambda name: name == "robovast-cluster")
    assert doctor.check_cluster_support() == []
