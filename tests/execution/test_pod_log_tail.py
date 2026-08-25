# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""PodLogTail — the incremental merge behind a cluster job's live log panel.

This had no coverage at all, which is how it survived the move to three containers: the
simulator and the system under test became NATIVE sidecars (init containers with
restartPolicy Always), the tail still enumerated ``spec.containers``, and the panel
quietly showed one container out of three. Nothing failed; there was simply less log.

The load-bearing property is that the buffer is **append-only**. A client holds a byte
offset into it and resumes from there across polls and reconnects, so a read that
reordered or rewrote earlier bytes would hand it the middle of a line it had already
seen. Every test here is ultimately about that.

No cluster needed: the kube API surface used is three calls wide.
"""

import types

import pytest

from robovast.execution.cluster_execution.cluster_execution import PodLogTail


def _pod(*sidecars, phase="Running"):
    """A scenario-run pod: the ``robovast`` container plus native sidecars by name."""
    init = [types.SimpleNamespace(name="s3-init", restart_policy=None)]
    init += [types.SimpleNamespace(name=n, restart_policy="Always") for n in sidecars]
    return types.SimpleNamespace(
        metadata=types.SimpleNamespace(name="pod-1"),
        spec=types.SimpleNamespace(
            containers=[types.SimpleNamespace(name="robovast")],
            init_containers=init),
        status=types.SimpleNamespace(phase=phase))


class _Core:
    """Scripted ``read_namespaced_pod_log``: ``{container: text}``, or an exception."""

    def __init__(self, logs):
        self.logs = logs
        self.calls = []

    def read_namespaced_pod_log(self, name, namespace, container, **kw):
        self.calls.append((container, kw.get("since_seconds")))
        value = self.logs.get(container, "")
        if isinstance(value, Exception):
            raise value
        return value


def _api_exception(status):
    from kubernetes import client
    return client.exceptions.ApiException(status=status)


def _ts(second, message):
    return f"2026-08-07T10:00:{second:02d}.000000000Z {message}\n"


def test_all_three_containers_reach_the_log_merged_by_timestamp():
    """The regression: sidecars live in initContainers and were never read.

    Ordering is by kubelet's per-line timestamp, not by container, so the causal story
    survives -- the sim's failure appears before the timeout it caused, not in a
    separate block after it.
    """
    core = _Core({
        "robovast": _ts(2, "executing scenario"),
        "simulation": _ts(1, "mujoco loaded"),
        "sut": _ts(3, "bt_navigator ready"),
    })
    tail = PodLogTail()
    tail.read(core, _pod("simulation", "sut"), "ns", now=1000.0)

    lines = bytes(tail.merged.buf).decode().splitlines()
    assert [ln.split("]")[0] + "]" for ln in lines] == [
        "[simulation]", "[robovast]", "[sut]"]
    assert "s3-init" not in bytes(tail.merged.buf).decode(), \
        "an ordinary init container is startup noise, not workload output"


def test_a_single_container_log_carries_no_prefix():
    """Nothing to disambiguate, so the prefix would be pure noise."""
    core = _Core({"robovast": _ts(1, "hello")})
    tail = PodLogTail()
    tail.read(core, _pod(), "ns", now=1000.0)
    assert bytes(tail.merged.buf).decode() == "hello\n"


def test_the_buffer_is_append_only_across_polls():
    """What the byte-offset protocol rests on: an earlier read's bytes never move."""
    core = _Core({"robovast": _ts(1, "a"), "simulation": _ts(2, "b")})
    pod = _pod("simulation")
    tail = PodLogTail()

    tail.read(core, pod, "ns", now=1000.0)
    first = bytes(tail.merged.buf)
    # A real since_seconds window OVERLAPS the previous one, so it re-delivers the line
    # the tail anchored on. Deduping against that anchor is what keeps this append-only.
    core.logs = {"robovast": _ts(1, "a") + _ts(3, "c"),
                 "simulation": _ts(2, "b") + _ts(4, "d")}
    tail.read(core, pod, "ns", now=1000.5)
    second = bytes(tail.merged.buf)

    assert second.startswith(first), "a poll rewrote bytes a client already consumed"
    assert b"c" in second and b"d" in second
    assert second.count(b" a\n") == 1, "the overlapping window was re-appended"


def test_a_container_that_has_not_started_is_skipped_not_fatal():
    """A Pending pod's scenario container 400s while its sidecar is already logging --
    the exact shape of the window this tail must now serve."""
    core = _Core({
        "robovast": _api_exception(400),
        "simulation": _ts(1, "loading world"),
    })
    tail = PodLogTail()
    tail.read(core, _pod("simulation", phase="Pending"), "ns", now=1000.0)
    assert "loading world" in bytes(tail.merged.buf).decode()


def test_an_unexpected_api_error_is_not_swallowed():
    """400/404 mean "no log yet"; anything else is a real failure and must surface as
    a streamerror rather than an eternally empty panel."""
    core = _Core({"robovast": _api_exception(500)})
    with pytest.raises(Exception):
        PodLogTail().read(core, _pod(), "ns", now=1000.0)


def test_containers_are_fetched_concurrently_not_serially():
    """Three serial round-trips per 0.5s poll -- over a port-forward, when the service
    drives the lane from off-cluster -- is enough to make the panel trail the run."""
    import threading

    barrier = threading.Barrier(3, timeout=5)

    class _BlockingCore(_Core):
        def read_namespaced_pod_log(self, name, namespace, container, **kw):
            # Only completes if all three are in flight at once; a serial
            # implementation deadlocks here and the barrier times out.
            barrier.wait()
            return super().read_namespaced_pod_log(name, namespace, container, **kw)

    core = _BlockingCore({n: _ts(1, n) for n in ("robovast", "simulation", "sut")})
    PodLogTail().read(core, _pod("simulation", "sut"), "ns", now=1000.0)


def test_a_later_poll_asks_only_for_the_elapsed_window():
    """The whole point of the tail: a long-running job's panel must not re-pull
    megabytes from the kube API every poll."""
    core = _Core({"robovast": _ts(1, "a")})
    pod = _pod()
    tail = PodLogTail()

    tail.read(core, pod, "ns", now=1000.0)
    assert core.calls[-1][1] is None, "first read pulls the whole log"
    tail.read(core, pod, "ns", now=1010.0)
    assert core.calls[-1][1] == 10 + PodLogTail._SINCE_SLACK


def test_terminal_follows_the_pod_phase():
    core = _Core({"robovast": _ts(1, "done")})
    assert PodLogTail().read(core, _pod(phase="Succeeded"), "ns", now=1000.0) is True
    assert PodLogTail().read(core, _pod(phase="Running"), "ns", now=1000.0) is False
