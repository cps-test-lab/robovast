# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""The aux container's workspace mirroring: how a copy-in is framed, and when it is skipped.

Nothing covered this before, and it was broken in a way that only shows up against a real cluster:
``_copy_in`` piped a base64 tarball into ``base64 -d | tar xzf -`` and relied on stdin EOF to end it.
The Kubernetes stream client can write stdin but cannot half-close it, so the receiver waited forever,
the exec never returned, and ``run()`` hung -- observed against a live pod on an *empty* workspace, for
2m47s, before it was killed. These tests pin the two properties that fix it.
"""

import base64
import io
import os
import tarfile

import pytest

from robovast.common.variation.container_runner import ContainerSpec
from robovast.execution.cluster_execution.container_runner import (
    ClusterContainerRunner, build_aux_pod_manifest)


def _runner():
    spec = ContainerSpec(image="example/img:1", keep_alive_command=["sleep", "infinity"])
    return ClusterContainerRunner(spec, "pod-x", "ns", core_v1=object())


class _Recorder:
    """Captures what would have been exec'd, instead of talking to a cluster."""

    def __init__(self):
        self.calls = []

    def __call__(self, command, stdin_data=None, progress_update_callback=None):
        self.calls.append((command, stdin_data))
        return ""


def test_copy_in_bounds_the_read_by_length_not_eof(monkeypatch):
    """``head -c <n>`` is what lets the receiver finish: stdin never sees an EOF.

    The exact byte count must match the payload the sender writes, or the pipeline either truncates
    the tar or goes back to waiting forever.
    """
    runner = _runner()
    with open(os.path.join(runner.workspace, "world.yaml"), "w", encoding="utf-8") as fh:
        fh.write("sim: {}\n")
    rec = _Recorder()
    monkeypatch.setattr(runner, "_retrying_exec", rec)

    runner._copy_in()

    (command, payload), = rec.calls
    assert command[:2] == ["sh", "-c"]
    script = command[2]
    assert f"head -c {len(payload)}" in script, "the read must be length-framed"
    assert "base64 -d" in script and "tar xzf -" in script
    # No EOF-dependent form survives: a bare `base64 -d` reading straight off stdin is the bug.
    assert "| base64 -d" in script and not script.strip().endswith("base64 -d")
    # And the payload really is the workspace, so the framing is not framing the wrong thing.
    raw = base64.b64decode(payload)
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        assert "./world.yaml" in tar.getnames()


def test_copy_in_of_an_empty_workspace_transfers_nothing(monkeypatch):
    """A generator whose inputs all live in its image stages no files.

    Sending an empty tarball is a round trip per ``run()`` for zero bytes -- and it is the case the
    hang was first seen on, so it is worth keeping honest.
    """
    runner = _runner()
    rec = _Recorder()
    monkeypatch.setattr(runner, "_retrying_exec", rec)

    runner._copy_in()

    (command, payload), = rec.calls
    assert payload is None, "nothing to send, so nothing is sent"
    assert command[2] == f"mkdir -p '{runner.workspace}'", "but the workspace still has to exist"


def test_copy_out_needs_no_stdin_at_all():
    """The pull direction is EOF-free by construction, which is why only copy-in was broken."""
    import inspect
    src = inspect.getsource(ClusterContainerRunner._copy_out)
    assert "stdin" not in src
    assert "tar czf -" in src and "base64" in src


def test_a_hung_helper_does_not_hang_the_campaign_forever(monkeypatch):
    """``_exec`` had no overall bound: it polled ``update(timeout=1)`` until the command
    ended, so a helper that never ended took the campaign's worker thread with it.

    It now shares the exec-lane's bounded stream, and a timeout is reported as the same
    failure type as any other non-zero exit, so the plugin contract is unchanged.
    """
    import subprocess

    runner = _runner()
    monkeypatch.setattr(
        "robovast.common.kube.exec_stream",
        lambda *a, **k: (124, "", "", True))
    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        runner._exec(["sleep", "infinity"])
    assert "exceeded" in str(excinfo.value.output)


def test_the_exec_bound_is_passed_through_not_ignored(monkeypatch):
    from robovast.execution.cluster_execution.container_runner import AUX_EXEC_LIMIT_S
    seen = {}

    def fake_stream(*_a, **kwargs):
        seen.update(kwargs)
        return 0, "ok", "", False

    runner = _runner()
    monkeypatch.setattr("robovast.common.kube.exec_stream", fake_stream)
    assert runner._exec(["true"]) == "ok"
    assert seen["limit_s"] == AUX_EXEC_LIMIT_S


def test_a_terminating_pod_is_waited_out_rather_than_adopted(monkeypatch):
    """A 409 used to mean "already exists -> reuse it".

    The name is derived from the campaign id, so the pod it collides with is this
    campaign's previous one -- usually still ``Terminating``, and a Terminating pod never
    becomes Running. Adopting it meant waiting out the full ready timeout for a corpse.
    """
    from kubernetes.client.rest import ApiException

    from robovast.execution.cluster_execution.container_runner import AuxPodSession

    events = []

    class _Core:
        def create_namespaced_pod(self, namespace, manifest):
            events.append("create")
            if events.count("create") == 1:
                raise ApiException(status=409, reason="AlreadyExists")

        def delete_namespaced_pod(self, name, namespace, **kwargs):
            events.append("delete")

        def read_namespaced_pod(self, name, namespace):
            raise AssertionError("should go through the shared helpers")

    monkeypatch.setattr(
        "robovast.common.kube.wait_pod_gone",
        lambda *a, **k: events.append("wait_gone"))
    monkeypatch.setattr(
        "robovast.common.kube.wait_pod_ready",
        lambda *a, **k: events.append("wait_ready"))
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.container_runner."
        "service_pod_owner_reference", lambda *a, **k: None)

    spec = ContainerSpec(image="example/img:1")
    session = AuxPodSession("c-1", [spec], "ns", core_v1=_Core())
    session.__enter__()

    assert events == ["create", "delete", "wait_gone", "create", "wait_ready"]


@pytest.mark.parametrize("secret", ["", "harbor-pull"])
def test_aux_pod_pull_secret_is_set_only_when_named(secret):
    """Aux images used to be public; a spec naming the campaign's own image is not.

    ``imagePullPolicy: IfNotPresent`` hides a missing secret on any node that already cached the
    image, so this fails first on a *fresh* node -- the worst place to discover it.
    """
    spec = ContainerSpec(image="harbor.example/robovast/campaign@sha256:abc")
    m = build_aux_pod_manifest("c-2026-08-06-000000", [spec], "ns", pull_secret=secret)
    if secret:
        assert m["spec"]["imagePullSecrets"] == [{"name": secret}]
    else:
        assert "imagePullSecrets" not in m["spec"]
