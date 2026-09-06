# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``vast exec cluster setup --performance-governor``.

A node on a scaling governor runs faster the busier it is, which makes every per-node figure
a campaign records a function of how much else was running. Opt-in, because unlike every
other thing setup installs this reconfigures the host rather than reporting a fact about it.
"""

import types

import pytest

from robovast.execution.cluster_execution.node_governor import (ABSENT, DAEMONSET_NAME, FAILED,
                                                                PERFORMANCE, REMOVED,
                                                                ensure_cpu_governor,
                                                                manifest, refusal_message,
                                                                remove_daemonset,
                                                                runtime_failure_message,
                                                                unavailable_message)


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
    """A cluster that takes the DaemonSet and, by default, runs it on its one node.

    *desired* / *ready* are what the DaemonSet controller reports. ``ready=0`` over a
    non-zero *desired* is the cloud-VM case: created successfully, then CrashLoopBackOff on
    every node because the guest has no writable cpufreq.
    """

    def __init__(self, create_error=None, desired=1, ready=1):
        self.created, self.replaced, self.deleted = [], [], []
        self._create_error = create_error
        self._desired, self._ready = desired, ready
        self.status_reads = 0

    def create_namespaced_daemon_set(self, namespace, body):
        if self._create_error:
            raise self._create_error
        self.created.append(body)

    def replace_namespaced_daemon_set(self, name, namespace, body):
        self.replaced.append(body)

    def read_namespaced_daemon_set_status(self, name, namespace):
        self.status_reads += 1
        return types.SimpleNamespace(status=types.SimpleNamespace(
            desired_number_scheduled=self._desired, number_ready=self._ready))

    def delete_namespaced_daemon_set(self, name, namespace, body=None):
        self.deleted.append(name)


def _instant(apps, **kwargs):
    """``ensure_cpu_governor`` with the readiness wait resolved without wall-clock time."""
    ticks = iter(range(0, 10_000, 10))
    return ensure_cpu_governor(apps, "default", True, sleep=lambda _s: None,
                               clock=lambda: next(ticks), ready_timeout_s=30, **kwargs)


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


# -- accepted, and then not running --------------------------------------------------------

def test_a_daemonset_that_never_runs_is_not_reported_as_applied(caplog):
    """The cloud-VM failure, and the one the API cannot report.

    A GCE or EC2 guest ACCEPTS the privileged pod -- only Autopilot-style clusters refuse it
    -- and then has no writable cpufreq, because the hypervisor owns the clock. The pod exits
    non-zero exactly as designed and CrashLoopBackOffs on every node, while the create call
    returned 201. Reporting that as applied leaves the operator trusting measurements taken on
    a scaling clock, which is the whole thing this module exists to prevent.
    """
    import logging

    apps = _Apps(desired=3, ready=0)
    with caplog.at_level(logging.WARNING):
        assert _instant(apps) is False
    assert "is not setting" in caplog.text
    assert "node image" in caplog.text, "the remedy on a managed pool, since nothing was refused"
    assert "cpu_governor_scaling" in caplog.text


def test_an_explicit_request_raises_when_it_is_accepted_and_does_not_run():
    """Same split as the refusal: someone who asked for a fixed clock and did not get one is
    worse off than someone who never asked."""
    with pytest.raises(RuntimeError, match="is not setting"):
        _instant(_Apps(desired=3, ready=0), explicit=True)


def test_a_selector_that_matches_no_node_says_so_rather_than_blaming_the_clock():
    """Nothing is scheduled, so nothing crashed -- a different mistake with a different fix,
    and one a message about cpufreq would send an operator hunting for."""
    apps = _Apps(desired=0, ready=0)
    assert _instant(apps) is False
    problem = runtime_failure_message(PERFORMANCE, "no node was selected")
    assert "cpufreq" not in problem


def test_a_running_daemonset_is_reported_once_and_not_waited_on():
    apps = _Apps(desired=2, ready=2)
    assert _instant(apps) is True
    assert apps.status_reads == 1, "a Ready DaemonSet must not cost the whole timeout"


def test_a_status_that_cannot_be_read_is_not_read_as_a_failure(caplog):
    """This runs after a successful create. Turning "could not check" into "did not work"
    would make a missing RBAC verb look like a cluster that cannot hold its clock."""
    import logging

    class _NoStatus(_Apps):
        def read_namespaced_daemon_set_status(self, name, namespace):
            raise _ApiException(403, "daemonsets/status is forbidden")

    with caplog.at_level(logging.WARNING):
        assert _instant(_NoStatus()) is True
    assert "could not read its status" in caplog.text, "unknown must not pass silently"


# -- a provider whose nodes cannot take one ------------------------------------------------

def test_a_vm_provider_declares_that_there_is_no_clock_to_fix():
    """Known before anything is applied, so setup should not spend a readiness wait per run
    rediscovering it."""
    from robovast.execution.cluster_config.base_config import BaseConfig
    from robovast.execution.cluster_config.azure import AzureClusterConfig
    from robovast.execution.cluster_config.gcp import GcpClusterConfig

    assert BaseConfig.governor_is_settable is True, (
        "attempting and reporting is the safe default; only a provider that KNOWS opts out")
    assert GcpClusterConfig.governor_is_settable is False
    assert AzureClusterConfig.governor_is_settable is False


def test_not_attempting_it_still_says_what_is_left_unfixed():
    """What the governor buys is comparability, and that need does not go away with the knob.
    A cluster measuring on a clock nobody fixed must not be a silent state."""
    message = unavailable_message(PERFORMANCE, "gcp")

    assert "cpu_governor_scaling" in message, "the per-campaign report must be named"
    assert "--performance-governor" in message, "and the way to override the default"
    assert "comparability" in message


def test_asking_for_nothing_removes_a_previously_configured_daemonset():
    """Setup writes the cluster's configuration on every run, so omitting the flag must take
    it away -- a governor still held by a deployment nobody remembers configuring is the same
    surprise as one silently reverting."""
    apps = _Apps()
    assert ensure_cpu_governor(apps, "default", False) is False
    assert apps.deleted == [DAEMONSET_NAME]


def test_removing_one_that_was_never_there_is_not_an_error():
    class _Missing(_Apps):
        def delete_namespaced_daemon_set(self, name, namespace, body=None):
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


# -- teardown ------------------------------------------------------------------------------

def test_cleanup_removes_the_daemonset_not_its_pods():
    """The pods are owned by the DaemonSet, so deleting them recreates them at once. Only
    removing the owner ends them -- and foreground propagation means the call does not
    return before they are going."""
    apps = _Apps()
    assert remove_daemonset(apps, "ns1") == REMOVED
    assert apps.deleted == [DAEMONSET_NAME]


def test_cleanup_is_idempotent_when_the_daemonset_is_already_gone():
    """A teardown must be re-runnable: a 404 is the end state cleanup wanted, not an error."""
    class _Missing(_Apps):
        def delete_namespaced_daemon_set(self, name, namespace, body=None):
            raise _ApiException(404, "not found")

    assert remove_daemonset(_Missing(), "ns1") == ABSENT


def test_cleanup_reports_a_failure_rather_than_raising_or_hiding_it():
    """A teardown must not abandon the remaining objects, and must not claim to have removed
    what is still running -- the caller prints the difference."""
    class _Broken(_Apps):
        def delete_namespaced_daemon_set(self, name, namespace, body=None):
            raise _ApiException(500, "boom")

    assert remove_daemonset(_Broken(), "ns1") == FAILED
