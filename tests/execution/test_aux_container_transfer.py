# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""The aux container's workspace mirroring: how it moves, and when it is skipped.

The history matters, because these tests are what keep it from coming back. ``_copy_in``
used to pipe a base64 tarball into ``base64 -d | tar xzf -`` and rely on stdin EOF to end
it. The Kubernetes stream client can write stdin but cannot half-close it, so the receiver
waited forever, the exec never returned, and ``run()`` hung -- observed against a live pod
on an *empty* workspace, for 2m47s, before it was killed. It was fixed by framing the read
with ``head -c <n>``.

The transfer now goes through the object store instead, the way a campaign Job and the
container-exec lane already stage: no stdin in either direction, so that failure mode is
gone by construction rather than by a correct byte count. What is pinned here is that
*absence*, the empty-workspace short-circuit that the hang was first seen on, and the
cleanup — a per-variation runner that leaked its prefix would accumulate all campaign long.
"""

import os

import pytest

from robovast.common.variation.container_runner import ContainerSpec
from robovast.execution.cluster_execution.container_runner import (
    AuxPodSession, ClusterContainerRunner, aux_owner_prefix, build_aux_pod_manifest,
    mc_host_env)

_S3 = ("http://robovast:9000", "minioadmin", "minioadmin")


class _FakeStore:
    def __init__(self):
        self.uploads, self.downloads, self.deleted = [], [], []

    def upload_dir(self, local_dir, bucket, prefix=""):
        self.uploads.append((local_dir, bucket, prefix))
        return 1

    def download_prefix(self, bucket, prefix, local_dir, force=False, on_file=None):
        self.downloads.append((bucket, prefix, local_dir, force))
        return 1

    def delete_prefix(self, bucket, prefix):
        self.deleted.append((bucket, prefix))
        return 1


def _runner(store=None, owner="c-2026-08-06-000000"):
    spec = ContainerSpec(image="example/img:1", keep_alive_command=["sleep", "infinity"])
    return ClusterContainerRunner(spec, "pod-x", "ns", core_v1=object(),
                                  storage=store if store is not None else _FakeStore(),
                                  bucket="robovast-image-builds", owner_id=owner)


class _Recorder:
    """Captures what would have been exec'd, instead of talking to a cluster."""

    def __init__(self):
        self.calls = []

    def __call__(self, command, stdin_data=None, progress_update_callback=None):
        self.calls.append((command, stdin_data))
        return ""


# -- the transfer -------------------------------------------------------------


def test_neither_direction_uses_stdin_at_all(monkeypatch):
    """The EOF hang cannot recur, because nothing is written to stdin any more.

    This is the strengthened successor to the ``head -c <n>`` test: that one checked the
    framing was *correct*, this one checks there is nothing left to frame.
    """
    store = _FakeStore()
    runner = _runner(store)
    with open(os.path.join(runner.workspace, "world.yaml"), "w", encoding="utf-8") as fh:
        fh.write("sim: {}\n")
    rec = _Recorder()
    monkeypatch.setattr(runner, "_retrying_exec", rec)

    runner._copy_in()
    runner._copy_out()

    assert [payload for _cmd, payload in rec.calls] == [None, None]
    for cmd, _payload in rec.calls:
        script = cmd[2]
        for gone in ("head -c", "base64", "tar xzf", "tar czf"):
            assert gone not in script, f"{gone!r} is the transport that was replaced"


def test_copy_in_uploads_then_mirrors_down(monkeypatch):
    store = _FakeStore()
    runner = _runner(store)
    with open(os.path.join(runner.workspace, "world.yaml"), "w", encoding="utf-8") as fh:
        fh.write("sim: {}\n")
    rec = _Recorder()
    monkeypatch.setattr(runner, "_retrying_exec", rec)

    runner._copy_in()

    (local, bucket, prefix), = store.uploads
    assert local == runner.workspace and bucket == "robovast-image-builds"
    (cmd, _), = rec.calls
    script = cmd[2]
    # Remote is the source, local the destination.
    assert f"'mystore/{bucket}/{prefix}/' '{runner.workspace}/'" in script
    assert "/tools/mc" in script, "mc comes from the injected copy, not the aux image"


def test_copy_out_mirrors_up_then_downloads_forcing_a_refresh(monkeypatch):
    """``force=True`` matters: the default skips a file whose size still matches, and a
    regenerated artifact of the same size is an ordinary outcome, not a curiosity."""
    store = _FakeStore()
    runner = _runner(store)
    rec = _Recorder()
    monkeypatch.setattr(runner, "_retrying_exec", rec)

    runner._copy_out()

    (cmd, _), = rec.calls
    assert f"'{runner.workspace}/' 'mystore/" in cmd[2], "local is the source going up"
    (bucket, prefix, local, force), = store.downloads
    assert local == runner.workspace and force is True


def test_the_mirror_overwrites_rather_than_skipping_matching_files(monkeypatch):
    """The transport this replaced extracted a tar over the destination, which always
    overwrote. Without ``--overwrite`` mc would quietly keep the stale copy."""
    runner = _runner()
    rec = _Recorder()
    monkeypatch.setattr(runner, "_retrying_exec", rec)
    runner._copy_out()
    assert "--overwrite" in rec.calls[0][0][2]


def test_copy_in_of_an_empty_workspace_transfers_nothing(monkeypatch):
    """A generator whose inputs all live in its image stages no files.

    Round-tripping zero bytes is pure latency per ``run()`` -- and it is the case the
    original hang was first seen on, so it is worth keeping honest.
    """
    store = _FakeStore()
    runner = _runner(store)
    rec = _Recorder()
    monkeypatch.setattr(runner, "_retrying_exec", rec)

    runner._copy_in()

    (command, payload), = rec.calls
    assert payload is None, "nothing to send, so nothing is sent"
    assert command[2] == f"mkdir -p '{runner.workspace}'", "but the workspace still exists"
    assert store.uploads == [], "and nothing reaches the store either"


def test_a_runner_without_a_store_refuses_instead_of_running_unstaged(monkeypatch):
    spec = ContainerSpec(image="example/img:1")
    runner = ClusterContainerRunner(spec, "pod-x", "ns", core_v1=object())
    with open(os.path.join(runner.workspace, "f.txt"), "w", encoding="utf-8") as fh:
        fh.write("x")
    monkeypatch.setattr(runner, "_retrying_exec", _Recorder())
    with pytest.raises(RuntimeError, match="object store"):
        runner._copy_in()


# -- isolation and cleanup ----------------------------------------------------


def test_two_runners_never_share_a_prefix():
    """A runner is built per *variation*, so two of them sharing one aux container would
    share a prefix — and whichever closed first would delete the other's files."""
    a, b = _runner(), _runner()
    assert a._prefix != b._prefix
    assert a._prefix.startswith(aux_owner_prefix("c-2026-08-06-000000"))


def test_close_drops_the_mirror_and_the_local_scratch():
    store = _FakeStore()
    runner = _runner(store)
    workspace = runner.workspace
    runner.close()
    assert store.deleted == [("robovast-image-builds", runner._prefix)]
    assert not os.path.exists(workspace), "the service's temp dir is not the pod's problem"


def test_a_failing_delete_does_not_fail_the_variation():
    class _Broken(_FakeStore):
        def delete_prefix(self, bucket, prefix):
            raise RuntimeError("store is down")

    runner = _runner(_Broken())
    runner.close()   # must not raise


def test_the_session_sweeps_what_a_crashed_runner_left(monkeypatch):
    """``close`` runs in a ``finally``, but not if the process died between them."""
    store = _FakeStore()
    session = AuxPodSession("c-2026-08-06-000000", [], "ns", core_v1=object(),
                            storage=store, bucket="b", s3=_S3)
    session._created = True
    monkeypatch.setattr(session, "_client",
                        lambda: type("C", (), {
                            "delete_namespaced_pod": lambda *a, **k: None})())
    session.__exit__(None, None, None)
    assert store.deleted == [("b", aux_owner_prefix("c-2026-08-06-000000"))]


def test_partial_store_wiring_is_refused_at_construction():
    """A pod with mc but no client (or the reverse) fails at the first run(), deep inside
    a plugin, instead of here where the cause is legible."""
    with pytest.raises(ValueError, match="together"):
        AuxPodSession("c-1", [], "ns", storage=_FakeStore(), bucket="b")


# -- the pod manifest ---------------------------------------------------------


def test_mc_is_injected_from_the_sidecar_not_required_of_the_aux_image():
    """The aux image belongs to a plugin author (``scenery_builder``); we cannot add
    tools to it, so the binary is copied in from the sidecar at pod creation."""
    from robovast.execution.cluster_execution.postprocess_job import SIDECAR_IMAGE
    spec = ContainerSpec(image="example/img:1")
    m = build_aux_pod_manifest("c-1", [spec], "ns", s3=_S3)["spec"]
    init, = m["initContainers"]
    assert init["image"] == SIDECAR_IMAGE
    assert "command -v mc" in init["command"][-1]
    assert m["volumes"] == [{"name": "aux-tools", "emptyDir": {}}]
    assert m["containers"][0]["volumeMounts"] == [
        {"name": "aux-tools", "mountPath": "/tools"}]


def test_the_mc_config_dir_is_world_writable():
    """An emptyDir belongs to root, and a spec's ``run_as_user`` means the container that
    has to run mc may be nobody in particular."""
    spec = ContainerSpec(image="example/img:1", run_as_user="1000:1000")
    m = build_aux_pod_manifest("c-1", [spec], "ns", s3=_S3)["spec"]
    assert "chmod 0777 /tools/mc-config" in m["initContainers"][0]["command"][-1]


def test_credentials_ride_in_mc_host_so_no_alias_command_is_needed():
    """``mc alias set`` writes to ``$HOME/.mc``; in an image that is not ours, HOME may
    not exist or not be writable, and run_as_user changes who is asking."""
    env = mc_host_env("http://robovast:9000", "key", "sec/ret+x")
    assert env["MC_HOST_mystore"] == "http://key:sec%2Fret%2Bx@robovast:9000", \
        "a secret with URL metacharacters must survive the round trip"


def test_the_spec_env_still_reaches_the_container():
    spec = ContainerSpec(image="example/img:1", env={"MY_VAR": "1"})
    m = build_aux_pod_manifest("c-1", [spec], "ns", s3=_S3)["spec"]
    env = {e["name"]: e["value"] for e in m["containers"][0]["env"]}
    assert env["MY_VAR"] == "1" and "MC_HOST_mystore" in env


def test_no_s3_means_no_tooling_is_attached():
    spec = ContainerSpec(image="example/img:1")
    m = build_aux_pod_manifest("c-1", [spec], "ns")["spec"]
    assert "initContainers" not in m and "volumes" not in m
    assert "volumeMounts" not in m["containers"][0]


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


# -- the exec bound (shared with the container-exec lane) ---------------------


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


# -- which cluster this talks to ----------------------------------------------


def test_the_session_honours_the_service_context(monkeypatch):
    """Without it, an aux pod lands in whichever cluster the *host* kubeconfig points at.

    That is not a small inconvenience: the campaign's helper containers would run
    somewhere else entirely while looking perfectly valid. The container-exec lane has had
    this test since a live cluster taught it the lesson; this path had the same fallback
    and no test, and it sent a standalone driver to a different cloud.
    """
    seen = {}
    monkeypatch.setattr("robovast.common.kube.load_kube_config",
                        lambda context=None: seen.update(context=context))
    monkeypatch.setattr("kubernetes.client.CoreV1Api", lambda: object())
    AuxPodSession("c-1", [], "ns", kube_context="local")._client()
    assert seen["context"] == "local"


def test_the_runner_honours_the_service_context(monkeypatch):
    seen = {}
    monkeypatch.setattr("robovast.common.kube.load_kube_config",
                        lambda context=None: seen.update(context=context))
    monkeypatch.setattr("kubernetes.client.CoreV1Api", lambda: object())
    spec = ContainerSpec(image="example/img:1")
    ClusterContainerRunner(spec, "pod-x", "ns", kube_context="local")._client()
    assert seen["context"] == "local"


def test_the_session_hands_its_context_to_the_runners_it_makes(monkeypatch):
    """The factory is where the two are joined; a runner that built its own client from
    the default context would reintroduce the bug one layer down."""
    session = AuxPodSession("c-1", [], "ns", core_v1=object(), kube_context="local")
    runner = session.runner_factory()(ContainerSpec(image="example/img:1"))
    assert runner._kube_context == "local"


@pytest.mark.parametrize("method", ["_campaign_context", "_scene_runner_context"])
def test_the_cluster_service_passes_its_own_context(method):
    """Both AuxPodSession call sites, because only one of them having it is the bug."""
    import inspect
    from robovast.service.cluster_service import ClusterService
    source = inspect.getsource(getattr(ClusterService, method))
    assert "kube_context=self.kube_context" in source
