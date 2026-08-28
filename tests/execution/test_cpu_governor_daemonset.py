# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``vast exec cluster setup --performance-governor``.

A node on a scaling governor runs faster the busier it is, which makes every per-node figure
a campaign records a function of how much else was running. Opt-in, because unlike every
other thing setup installs this reconfigures the host rather than reporting a fact about it.
"""

import types

import pytest

from robovast.execution.cluster_execution.node_governor import (DAEMONSET_NAME, PERFORMANCE,
                                                                ensure_cpu_governor,
                                                                manifest, refusal_message)


class _ApiException(Exception):
    def __init__(self, status, body=""):
        super().__init__(body)
        self.status = status
        self.body = body


@pytest.fixture(autouse=True)
def _api_exception(monkeypatch):
    """The module imports the real exception lazily; give it ours."""
    fake = types.ModuleType("kubernetes.client.exceptions")
    fake.ApiException = _ApiException
    monkeypatch.setitem(__import__("sys").modules, "kubernetes.client.exceptions", fake)


class _Apps:
    def __init__(self, create_error=None):
        self.created, self.replaced, self.deleted = [], [], []
        self._create_error = create_error

    def create_namespaced_daemon_set(self, namespace, body):
        if self._create_error:
            raise self._create_error
        self.created.append(body)

    def replace_namespaced_daemon_set(self, name, namespace, body):
        self.replaced.append(body)

    def delete_namespaced_daemon_set(self, name, namespace):
        self.deleted.append(name)


# -- the manifest --------------------------------------------------------------------------

def test_every_policy_is_written_not_just_cpu0():
    """A governor set on cpu0 alone leaves the rest of the machine scaling, which looks like
    a partial success rather than the no-op it is."""
    script = manifest("default", PERFORMANCE)["spec"]["template"]["spec"][
        "containers"][0]["command"][2]
    assert "/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor" in script
    assert PERFORMANCE in script


def test_it_fails_loudly_when_no_policy_accepts_the_value():
    """A DaemonSet sitting Ready over a cluster it never changed is the worst outcome: the
    operator would believe their measurements were taken on a fixed clock."""
    script = manifest("default", PERFORMANCE)["spec"]["template"]["spec"][
        "containers"][0]["command"][2]
    assert "exit 1" in script


def test_the_setting_is_re_asserted_rather_than_written_once():
    """A rebooted node comes back on its own default, and a governor that silently reverts is
    worse than one never set."""
    script = manifest("default", PERFORMANCE)["spec"]["template"]["spec"][
        "containers"][0]["command"][2]
    assert "while true" in script and "sleep" in script


def test_it_is_confined_to_the_campaign_node_pool_when_there_is_one():
    """A cluster that runs campaigns on a subset must not have its other machines
    reconfigured as a side effect of a RoboVAST setup."""
    spec = manifest("default", PERFORMANCE, {"pool": "batch"})["spec"]["template"]["spec"]
    assert spec["nodeSelector"] == {"pool": "batch"}
    assert "nodeSelector" not in manifest("default", PERFORMANCE)["spec"]["template"]["spec"]


# -- failure policy ------------------------------------------------------------------------

def test_a_cluster_that_forbids_privileged_pods_gets_the_managed_cluster_answer():
    """GKE/EKS/AKS refuse this, and there node auto-repair would undo it anyway. Retrying
    does not help, so the message must send the operator to the node image instead."""
    message = refusal_message(PERFORMANCE, "violates PodSecurity restricted", forbidden=True)
    assert "GKE" in message and "node image" in message
    assert "cpu_governor_scaling" in message, "the supported alternative must be named"


def test_an_ordinary_api_failure_is_not_dressed_up_as_unsupported():
    """Two different remedies, so they must not be merged: this one says nothing about
    whether the governor could have been set."""
    message = refusal_message(PERFORMANCE, "connection reset", forbidden=False)
    assert "GKE" not in message
    assert "the deploy failing, not the setting" in message


def test_an_explicit_request_that_cannot_be_honoured_raises():
    """The rule ensure_nvidia_device_plugin sets for an explicit request, and for the same
    reason: silently not getting a fixed clock is worse than never asking, because the
    operator would then trust measurements taken on a scaling one."""
    apps = _Apps(create_error=_ApiException(403, "privileged containers are not allowed"))
    with pytest.raises(RuntimeError, match="could not set the CPU governor"):
        ensure_cpu_governor(apps, "default", True, explicit=True)


def test_the_default_warns_and_carries_on_where_the_cluster_refuses(caplog):
    """It is ON by default, so a refusal must not fail setup: managed Kubernetes forbids
    privileged pods, and a default that failed there would make `setup` impossible on those
    clusters over an improvement nobody asked for. The refusal is still reported, and a
    campaign whose nodes still scale is caught by the `cpu_governor_scaling` advice -- so
    warning is not the same as silence."""
    import logging

    apps = _Apps(create_error=_ApiException(403, "privileged containers are not allowed"))
    with caplog.at_level(logging.WARNING):
        assert ensure_cpu_governor(apps, "default", True) is False
    assert "could not set the CPU governor" in caplog.text


def test_asking_for_nothing_removes_a_previously_configured_daemonset():
    """Setup writes the cluster's configuration on every run, so omitting the flag must take
    it away -- a governor still held by a deployment nobody remembers configuring is the same
    surprise as one silently reverting."""
    apps = _Apps()
    assert ensure_cpu_governor(apps, "default", False) is False
    assert apps.deleted == [DAEMONSET_NAME]


def test_removing_one_that_was_never_there_is_not_an_error():
    class _Missing(_Apps):
        def delete_namespaced_daemon_set(self, name, namespace):
            raise _ApiException(404, "not found")

    assert ensure_cpu_governor(_Missing(), "default", False) is False


def test_a_second_setup_replaces_rather_than_failing():
    apps = _Apps(create_error=_ApiException(409, "already exists"))
    assert ensure_cpu_governor(apps, "default", True) is True
    assert len(apps.replaced) == 1


def test_the_governor_is_not_selectable():
    """Performance is the only setting that serves the purpose -- a measurement cluster wants
    a clock that does not move -- and offering the choice would invite 'powersave' (the thing
    being fixed) or a typo that is unavailable on some drivers and a silent no-op on others."""
    import inspect

    params = inspect.signature(ensure_cpu_governor).parameters
    assert params["enabled"].annotation is bool


def test_a_plain_setup_installs_nothing():
    """There is no implicit path: unlike GPU provisioning, this never happens opportunistically."""
    apps = _Apps()
    assert ensure_cpu_governor(apps, "default", False) is False
    assert apps.created == []


# -- per-node calibration default ----------------------------------------------------------

def test_calibration_is_on_unless_explicitly_disabled(monkeypatch):
    """Flipped to on after a matched pair of 200-run campaigns: ~8% faster with ZERO
    control-loop misses in either arm, so the calibrated ceilings -- as low as 0.53 cores
    against a declared 3.0 -- did not starve the stack, which is the failure that had kept it
    off. See node_calibration's module docstring for the table.

    Unset reads as ON so an operator who never touched it gets what setup configured, and a
    typo reads as ON rather than silently disabling a feature the cluster was set up with --
    the same direction of safety the rest of this file follows.
    """
    from robovast.execution.cluster_execution.node_calibration import (CALIBRATION_ENV,
                                                                       calibration_enabled)

    monkeypatch.delenv(CALIBRATION_ENV, raising=False)
    assert calibration_enabled() is True

    for off in ("0", "false", "no", "off", "OFF"):
        monkeypatch.setenv(CALIBRATION_ENV, off)
        assert calibration_enabled() is False, off

    for on in ("1", "true", "on", "", "garbage"):
        monkeypatch.setenv(CALIBRATION_ENV, on)
        assert calibration_enabled() is True, on
