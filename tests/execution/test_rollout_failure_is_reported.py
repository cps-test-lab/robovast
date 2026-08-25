# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""An upgrade whose new pod cannot start says so, instead of waiting in silence.

``wait_for_rollout`` used to watch the Deployment's replica counters and nothing else. A
counter cannot fail, so an incoming pod in ``ImagePullBackOff`` -- a reason the kubelet
already had, and which k9s showed the whole time -- was three minutes of no output followed
by ``False``, which the caller then printed "✓ upgraded and ready" over anyway.

The reason is right there on the pod. Two things follow from that and are pinned here: it is
reported on the *first* poll (the silence is the bug, independently of when we give up), and
a rollout that never converges raises rather than returning a verdict a caller has to
remember to check.
"""

from types import SimpleNamespace
from unittest import mock

import pytest

from robovast.execution.cluster_execution import service_deploy
from robovast.execution.cluster_execution.service_deploy import RolloutNotConverged


def _seq(*values):
    """A ``side_effect`` that walks *values*, then repeats the last one forever.

    Repeating rather than running out: these loops poll until they can decide, so a test
    should pin the decision and not the number of polls it took. An ``Exception`` in
    *values* is raised when reached, which is how a failed probe is staged.
    """
    remaining = list(values)

    def next_value(*_args, **_kwargs):
        value = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        if isinstance(value, Exception):
            raise value
        return value
    return next_value


def _deployment(*, generation=1, observed=1, want=1, updated=1, replicas=1, available=1):
    """A Deployment status. The defaults describe a converged one."""
    return SimpleNamespace(
        metadata=SimpleNamespace(generation=generation),
        spec=SimpleNamespace(replicas=want),
        status=SimpleNamespace(observed_generation=observed, updated_replicas=updated,
                               replicas=replicas, available_replicas=available))


def _rolling():
    """Mid-rollout: the spec is observed, the new pod is not Available, the old one is up.

    This is the state a blocked upgrade sits in indefinitely -- and it is indistinguishable,
    from the counters alone, from one that is merely slow.
    """
    return _deployment(updated=1, replicas=2, available=1)


def _pod(created, *, waiting_reason=None, message="", restart_count=0, phase="Pending"):
    waiting = (SimpleNamespace(reason=waiting_reason, message=message)
               if waiting_reason else None)
    container = SimpleNamespace(
        name="robovast-service",
        state=SimpleNamespace(waiting=waiting, terminated=None),
        last_state=SimpleNamespace(terminated=SimpleNamespace(reason="Error", exit_code=1)),
        restart_count=restart_count)
    return SimpleNamespace(
        metadata=SimpleNamespace(creation_timestamp=created),
        status=SimpleNamespace(
            phase=phase,
            # pod_block_reason checks the PodScheduled condition after the containers, so
            # the attribute has to exist even on a pod that scheduled fine.
            conditions=[SimpleNamespace(type="PodScheduled", status="True", reason=None,
                                        message="")],
            init_container_statuses=None,
            container_statuses=[container]))


def _pods(*pod_objects):
    """What one ``list_namespaced_pod`` call returns."""
    return SimpleNamespace(items=list(pod_objects))


class _Clock:
    """A monotonic clock that advances only when the loop under test sleeps.

    Wall-clock time makes the grace window untestable: with a real clock the only way to
    keep a test fast is ``unhealthy_grace_s=0.0``, and at zero the window collapses -- every
    bug that merely *delays* the verdict by a poll or two still produces the same outcome.
    (A reset-on-failed-probe bug was reintroduced under exactly such a test and it passed.)
    Here one poll is one second, so timings are exact and a delay is visible.
    """

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(service_deploy, "_load_kube_config", lambda ctx=None: None)
    fake = _Clock()
    monkeypatch.setattr("time.monotonic", fake.monotonic)
    monkeypatch.setattr("time.sleep", fake.sleep)
    return fake


def _cluster(monkeypatch, deployments, pods=None):
    """Wire both k8s clients to *deployments* / *pods* (each a :func:`_seq` callable)."""
    apps, core = mock.Mock(), mock.Mock()
    apps.read_namespaced_deployment_status.side_effect = deployments
    core.list_namespaced_pod.side_effect = pods or _seq(_pods())
    monkeypatch.setattr("kubernetes.client.AppsV1Api", lambda: apps)
    monkeypatch.setattr("kubernetes.client.CoreV1Api", lambda: core)
    return apps, core


def test_a_blocked_pod_is_reported_on_the_first_poll(monkeypatch):
    """The anti-hang guarantee, and it holds regardless of when we give up waiting.

    The original complaint was not that the upgrade took minutes -- pulling a controller
    image legitimately does -- but that it did so with no output at all, which cannot be
    told apart from a hang.
    """
    _cluster(monkeypatch, _seq(_rolling()),
             _seq(_pods(_pod(1, waiting_reason="ImagePullBackOff",
                             message='Back-off pulling image "harbor.example/robovast/x"'))))
    said = []

    with pytest.raises(RolloutNotConverged):
        service_deploy.wait_for_rollout(timeout_s=5, unhealthy_grace_s=0.0,
                                        report=said.append)

    assert said, "a blocked rollout must not be silent"
    assert "ImagePullBackOff" in said[0]
    assert "harbor.example" in said[0], "Kubernetes' own message names the failing ref"


def test_still_blocked_past_the_grace_window_fails(monkeypatch):
    _cluster(monkeypatch, _seq(_rolling()),
             _seq(_pods(_pod(1, waiting_reason="ImagePullBackOff",
                             message="no pull access"))))

    with pytest.raises(RolloutNotConverged) as excinfo:
        service_deploy.wait_for_rollout(timeout_s=30, unhealthy_grace_s=0.0)

    message = str(excinfo.value)
    assert "ImagePullBackOff" in message
    assert "no pull access" in message, "Kubernetes' text is the diagnosis"
    # An image reason is a credentials question, so the message names where they come from.
    assert "ROBOVAST_REGISTRY_PASSWORD" in message
    assert "CURRENT directory" in message, "the CWD-only .env rule is the trap here"
    # And it must say the service is still up, or a failed upgrade reads as an outage.
    assert "still serving" in message
    assert "kubectl" in message, "must name how to look further"


def test_a_blocked_pod_that_recovers_inside_the_grace_window_succeeds(monkeypatch):
    """kubelet pull back-off does clear on its own -- which is why there is a grace window.

    The real run this was written from recovered after a few minutes, so aborting on first
    sight of ImagePullBackOff would have failed an upgrade that worked.
    """
    _cluster(monkeypatch,
             _seq(_rolling(), _rolling(), _deployment()),
             _seq(_pods(_pod(1, waiting_reason="ImagePullBackOff")),
                  _pods(_pod(1, phase="Running"))))

    service_deploy.wait_for_rollout(timeout_s=30, unhealthy_grace_s=60.0)


def test_a_crash_looping_pod_is_caught_too(monkeypatch):
    """``CrashLoopBackOff`` is in no constant set in this repo.

    Without the restart check a container that comes up and dies is invisible: it is never
    in a blocked ``waiting`` state, so it would fall through to a bare timeout carrying no
    reason -- the same silent wait, a different cause.
    """
    _cluster(monkeypatch, _seq(_rolling()),
             _seq(_pods(_pod(1, phase="Running", restart_count=3))))

    with pytest.raises(RolloutNotConverged) as excinfo:
        service_deploy.wait_for_rollout(timeout_s=30, unhealthy_grace_s=0.0)

    assert "restarted 3x" in str(excinfo.value)
    # Not a credentials problem: pointing at the registry here would send the reader off to
    # audit variables that are working fine.
    assert "ROBOVAST_REGISTRY_PASSWORD" not in str(excinfo.value)


def test_a_failed_probe_does_not_reset_the_grace_timer(monkeypatch):
    """"Could not check" is *unknown*, not *healthy*.

    Clearing the stamp on an unreadable poll restarts the grace window every time the
    apiserver blips, so the precise verdict arrives late or not at all -- the regression the
    campaign batch loop documents at kubernetes_backend.py.

    The harm is visible only when the delay pushes the verdict past the deadline: the
    operator then gets a vague "did not converge" timeout instead of "will not recover, here
    is Kubernetes' reason". So the window is sized to land just inside the timeout, and one
    reset is enough to lose it.
    """
    blocked = _pods(_pod(1, waiting_reason="ImagePullBackOff"))
    _cluster(monkeypatch, _seq(_rolling()),
             _seq(blocked, RuntimeError("apiserver unreachable"), blocked))

    with pytest.raises(RolloutNotConverged) as excinfo:
        service_deploy.wait_for_rollout(timeout_s=6, unhealthy_grace_s=5.0)

    message = str(excinfo.value)
    assert "will not recover" in message, (
        "a reset stamp delays the verdict past the deadline, downgrading a precise "
        f"diagnosis to a bare timeout: {message}")
    assert "ImagePullBackOff" in message


def test_the_newest_pod_is_the_one_judged(monkeypatch):
    """A rolling update leaves the old pod Running; it must not mask the new one's block."""
    _cluster(monkeypatch, _seq(_rolling()),
             _seq(_pods(_pod(1, phase="Running"),
                        _pod(2, waiting_reason="CreateContainerConfigError"))))

    with pytest.raises(RolloutNotConverged, match="CreateContainerConfigError"):
        service_deploy.wait_for_rollout(timeout_s=30, unhealthy_grace_s=0.0)


def test_a_converged_rollout_returns_quietly(monkeypatch):
    """No news on the happy path: the heartbeat is for waiting, not for succeeding."""
    _cluster(monkeypatch, _seq(_deployment()))
    said = []

    service_deploy.wait_for_rollout(timeout_s=5, report=said.append)

    assert said == []


def test_a_timeout_with_no_pod_error_points_at_the_logs(monkeypatch):
    """Nothing wrong on the pod's status means it is still starting -- or never Ready.

    Naming a registry problem here would be a guess; the logs are where the answer is.
    """
    _cluster(monkeypatch, _seq(_rolling()), _seq(_pods(_pod(1, phase="Running"))))

    with pytest.raises(RolloutNotConverged) as excinfo:
        service_deploy.wait_for_rollout(timeout_s=0.0, unhealthy_grace_s=60.0)

    message = str(excinfo.value)
    assert "did not converge" in message
    assert "logs" in message
    assert "--timeout" in message, "must name the escape hatch for a slow registry"


def test_the_heartbeat_does_not_repeat_the_reason(monkeypatch):
    """Found by running it, not by these tests: the reason was printed five times.

    kubelet alternates ``ErrImagePull`` and ``ImagePullBackOff`` while it backs off, and the
    message is ~300 characters, so echoing it on every heartbeat buried the actual diagnosis
    in copies of itself. It is reported once, in full, when it first appears.
    """
    blocked = _pods(_pod(1, waiting_reason="ImagePullBackOff", message="x" * 200))
    _cluster(monkeypatch, _seq(_rolling()), _seq(blocked))
    said = []

    with pytest.raises(RolloutNotConverged):
        service_deploy.wait_for_rollout(timeout_s=90, unhealthy_grace_s=45.0,
                                        report=said.append)

    heartbeats = [line for line in said if line.startswith("still not starting")]
    assert heartbeats, "a long wait must still show it is alive"
    assert all("x" * 200 not in line for line in heartbeats), (
        f"the reason must be reported once, not on every heartbeat: {heartbeats}")


def test_the_heartbeat_counts_toward_the_deadline_that_will_fire(monkeypatch):
    """It said "46s/180s" and then died at 60s, which is just untrue.

    Once the pod is unhealthy the grace window is what ends the wait, so that is the budget
    worth showing -- it answers "how long until this gives up?".
    """
    blocked = _pods(_pod(1, waiting_reason="ImagePullBackOff"))
    _cluster(monkeypatch, _seq(_rolling()), _seq(blocked))
    said = []

    with pytest.raises(RolloutNotConverged):
        service_deploy.wait_for_rollout(timeout_s=180, unhealthy_grace_s=30.0,
                                        report=said.append)

    heartbeats = [line for line in said if line.startswith("still not starting")]
    assert heartbeats
    assert all("/30s" in line for line in heartbeats), heartbeats
    assert all("180s" not in line for line in heartbeats), (
        f"the overall timeout is not what ends this wait: {heartbeats}")


def test_a_healthy_slow_pull_still_reports_progress(monkeypatch):
    """The complaint was silence, and a slow-but-fine pull is silent too.

    Nothing is wrong on the pod, so there is no reason to report -- but a minutes-long wait
    with no output is exactly what could not be told apart from a hang.
    """
    _cluster(monkeypatch, _seq(_rolling()), _seq(_pods(_pod(1, phase="Pending"))))
    said = []

    with pytest.raises(RolloutNotConverged):
        service_deploy.wait_for_rollout(timeout_s=40, unhealthy_grace_s=60.0,
                                        report=said.append)

    assert any(line.startswith("waiting for the new pod") for line in said), said
    assert any("Pending" in line for line in said), "say what it is doing, not just that it is"


# --- what to do next -------------------------------------------------------------------
#
# A suggested command has to be one that runs. These pin the two ways that goes wrong: a
# command aimed at the wrong cluster, and one generic hint offered for every failure.

def test_a_suggested_kubectl_names_the_cluster_it_means():
    """Without --context it targets the kubeconfig's current-context, not this cluster.

    Not hypothetical: on this host the active context is a remote cluster that is not
    reachable, so the pasted command hangs for 30s and then blames the wrong cluster --
    while the upgrade it was diagnosing ran against '-x local' all along.
    """
    hint = service_deploy._next_step("ImagePullBackOff: nope", "default", "local")

    assert "kubectl --context local -n default" in hint, hint


def test_a_suggested_kubectl_omits_the_flag_when_there_is_no_context():
    """`-x` is optional, and `--context ''` is not a valid command."""
    hint = service_deploy._next_step("ImagePullBackOff: nope", "default", None)

    assert "kubectl -n default" in hint
    assert "--context" not in hint


@pytest.mark.parametrize("signal,expected,forbidden", [
    # An image fault is a config question, so it leads with the vast command that checks
    # the config -- running an upgrade proves the reader has vast, not a kubeconfig.
    ("ImagePullBackOff: no pull access", "vast doctor", "--previous"),
    # The crash is in the container that already died, so the current pod's log is empty of
    # it. This is the one hint whose omission sends the reader to look at nothing.
    ("ContainerRestarted: container robovast-service restarted 3x", "--previous", "vast doctor"),
    # Capacity, not configuration: the scheduler already named the resource, and there is
    # nothing on this host to check.
    ("Unschedulable: 0/1 nodes are available: 1 Insufficient nvidia.com/gpu",
     "names what no node could satisfy", "vast doctor"),
])
def test_each_failure_gets_the_action_that_fits_it(signal, expected, forbidden):
    """Four states, four actions -- as `_status_next_step` in the MCP layer branches.

    One generic "inspect the pod" is a dead end for the reader who already did.
    """
    hint = service_deploy._next_step(signal, "default", "local")

    assert expected in hint, hint
    assert forbidden not in hint, f"{signal} must not be sent down the {forbidden} path: {hint}"


def test_only_an_image_fault_talks_about_credentials():
    """Auditing working credentials is a waste of the reader's time."""
    assert "ROBOVAST_REGISTRY_PASSWORD" in service_deploy._blocked_message(
        "default", "ErrImagePull: unauthorized", 60.0, "local")
    assert "ROBOVAST_REGISTRY_PASSWORD" not in service_deploy._blocked_message(
        "default", "Unschedulable: no node has 96 CPUs", 60.0, "local")
