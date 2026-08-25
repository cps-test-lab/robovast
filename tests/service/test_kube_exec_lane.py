# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Regression tests for the in-cluster exec lane.

The first group pins bugs found by running the lane against a live cluster, all of which
were invisible to the local lane:

- the kube context was ignored, so exec talked to whichever cluster the *kubeconfig*
  pointed at rather than the one the service runs campaigns on;
- ``stop_held`` returned while the pod was still ``Terminating``, so the next start hit
  ``AlreadyExists``;
- the "is anything running?" probe counted its own helper processes, so a pod was never
  idle and never idle-reaped.

The second group covers staging. ``/config`` arrives the way a campaign Job's does:
staged to the object store, mirrored down by an ``mc`` init container. As a ConfigMap it
would cap the staged tree at ~900 KiB and send the caller off to "run this config as a
campaign instead" — the cost the tool exists to avoid.

No cluster is needed here: these check the manifests, the argv and the call sequence.
"""

import pytest

from robovast.execution.cluster_execution.kube_exec_lane import KubeExecLane, exec_prefix
from robovast.service import container_exec as ce

_S3 = ("http://robovast:9000", "minioadmin", "minioadmin")


def _spec(tmp_path, command="ls", image="img:1", workspace=False):
    config = tmp_path / "config"
    config.mkdir()
    (config / "entrypoint.sh").write_text("#!/bin/bash\n")
    (config / "scenario.config").write_text("{}\n")
    nested = config / "files"
    nested.mkdir()
    (nested / "node.py").write_text("print(1)\n")
    kwargs = {}
    if workspace:
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "world.yaml").write_text("sim: {}\n")
        kwargs = {"workspace_dir": str(ws), "workspace_id": "ws-1"}
    return ce.ExecSpec(image=image, command=command, config_dir=str(config),
                       env={"OUTPUT_DIR": ce.OUTPUT_DIR}, config_name="c1", **kwargs)


class _FakeStore:
    """Records what would have been uploaded/deleted, instead of talking to S3."""

    def __init__(self):
        self.uploads = []
        self.deleted = []

    def upload_dir(self, local_dir, bucket, prefix=""):
        self.uploads.append((local_dir, bucket, prefix))
        return 1

    def delete_prefix(self, bucket, prefix):
        self.deleted.append((bucket, prefix))
        return 1


def _lane(store=None, namespace="ns"):
    return KubeExecLane(namespace, storage=store if store is not None else _FakeStore(),
                        bucket="robovast-image-builds", s3_endpoint=_S3[0],
                        s3_access_key=_S3[1], s3_secret_key=_S3[2])


def _manifest(spec, deadline=300, namespace="ns", owner=None, prefix=None,
              pull_secret=""):
    from robovast.execution.cluster_execution.kube_exec_lane import _pod_manifest
    return _pod_manifest(spec, deadline, namespace, owner, _S3,
                         "robovast-image-builds", prefix or exec_prefix(namespace),
                         pull_secret=pull_secret)


# -- pinned live-cluster bugs -------------------------------------------------


def test_the_service_kube_context_is_honoured(monkeypatch):
    """Without this, exec runs against the kubeconfig's current context.

    That is not a small inconvenience: the answer would come from a different cluster
    than the campaigns run on, while looking perfectly valid.
    """
    seen = {}

    def fake_load(context=None):
        seen["context"] = context

    monkeypatch.setattr(
        "robovast.execution.cluster_execution.kube_client.load_kube_config", fake_load)
    monkeypatch.setattr("kubernetes.client.CoreV1Api", lambda: object())
    KubeExecLane("ns", kube_context="local")._client()
    assert seen["context"] == "local"


def test_the_cluster_service_passes_its_own_context():
    import inspect

    from robovast.execution.cluster_execution.cluster_service import ClusterService
    source = inspect.getsource(ClusterService._exec_lane)
    assert "kube_context=self.kube_context" in source


def test_stopping_waits_for_the_pod_to_actually_be_gone():
    """A Kubernetes delete returns while the pod is still terminating.

    ``stop_held`` must offer the local lane's contract — ``docker rm -f`` is synchronous —
    or the single-container rule breaks: the next start collides with the corpse.
    """
    import inspect
    source = inspect.getsource(KubeExecLane.stop_held)
    assert "wait_pod_gone" in source
    wait = inspect.getsource(__import__(
        "robovast.execution.cluster_execution.kube_client", fromlist=["x"]).wait_pod_gone)
    assert "read_namespaced_pod" in wait
    assert "404" in wait, "absence is how it knows deletion finished"


def test_the_process_probe_spawns_nothing_of_its_own():
    """The probe must not count its own helpers.

    The first version piped ``ls`` into ``wc`` and saw four processes in an *idle* pod,
    so ``held_workload_running`` was permanently true and nothing was ever idle-reaped.
    Shell builtins only, and PID 1 / ``$$`` / ``$PPID`` excluded, so idle reads 0.
    """
    probe = KubeExecLane._PROCESS_COUNT_SH
    for spawned in ("ls ", "wc", "ps ", "pgrep", "awk", "grep"):
        assert spawned not in probe, f"the probe spawns {spawned!r} and would count it"
    assert '[ "$pid" = 1 ]' in probe
    assert '[ "$pid" = "$$" ]' in probe
    assert '[ "$pid" = "$PPID" ]' in probe


def test_the_probe_threshold_treats_zero_as_idle():
    import inspect
    assert "count > 0" in inspect.getsource(KubeExecLane.held_workload_running)


# -- staging through the object store ----------------------------------------


def test_the_whole_config_tree_is_staged_without_rewriting_names(tmp_path):
    """The ConfigMap had to flatten ``files/node.py`` to ``files__node.py`` and restore it.

    An object prefix has no such restriction, so the tree goes up as-is. This asserts the
    lane hands the directory over whole rather than reintroducing per-key encoding.
    """
    store = _FakeStore()
    prefix = _lane(store)._stage(_spec(tmp_path))
    assert prefix == exec_prefix("ns")
    (local_dir, bucket, key), = store.uploads
    assert local_dir == str(tmp_path / "config")
    assert bucket == "robovast-image-builds"
    assert key == f"{prefix}/config"


def test_a_config_larger_than_a_configmap_now_stages_fine(tmp_path):
    """The inversion that motivated the change.

    A ConfigMap-staged tree raises ``ValueError`` naming the ConfigMap limit and tells the
    caller to run a campaign instead — the expense ``exec_in_container`` exists to save.
    """
    spec = _spec(tmp_path)
    (tmp_path / "config" / "huge.bin").write_text("x" * (2 * 1024 * 1024))
    store = _FakeStore()
    _lane(store)._stage(spec)   # must not raise
    assert store.uploads, "the oversized tree is staged, not refused"


def test_staging_without_a_store_refuses_instead_of_running_unstaged(tmp_path):
    """No silent fallback: an unstaged /config answers a different question and looks OK."""
    lane = KubeExecLane("ns")   # no storage, no bucket
    with pytest.raises(RuntimeError, match="object store"):
        lane._stage(_spec(tmp_path))


def test_the_workspace_is_staged_under_its_own_prefix(tmp_path):
    """Parity with the local lane, which bind-mounts it at ``/sources/<id>``.

    A cluster lane ignoring ``workspace_dir`` answers the same call differently from the
    local one — locally the files are there, in-cluster they are not.
    """
    store = _FakeStore()
    lane = _lane(store)
    prefix = lane._stage(_spec(tmp_path, workspace=True))
    keys = [key for _local, _bucket, key in store.uploads]
    assert keys == [f"{prefix}/config", f"{prefix}/workspace"]


def test_stopping_discards_the_staged_tree(tmp_path):
    """A prefix nothing reaps is a leak, and the pod's owner reference cannot collect it."""
    store = _FakeStore()
    lane = _lane(store)
    lane._discard_staged()
    assert store.deleted == [("robovast-image-builds", exec_prefix("ns"))]


def test_the_prefix_is_namespaced(tmp_path):
    """The pod name is fixed, so it is unique per namespace — and so must the prefix be,
    since a shared-bucket deployment gives several namespaces one bucket."""
    assert exec_prefix("team-a") != exec_prefix("team-b")


# -- the pod manifest ---------------------------------------------------------


def test_the_init_container_is_the_sidecar_not_the_image_under_test(tmp_path):
    """Staging must not depend on what the experiment image happens to install.

    The sidecar carries ``mc``; the image under test may be anything, including one
    without it. (The old restore step ran the experiment image because a ConfigMap needed
    only ``cp``.)
    """
    from robovast.common.execution import resolve_sidecar_image
    init, = _manifest(_spec(tmp_path))["spec"]["initContainers"]
    assert init["name"] == "s3-init"
    assert init["image"] == resolve_sidecar_image()
    assert init["image"] != "img:1"
    script = init["command"][-1]
    assert "mc mirror" in script and "/config/" in script


def test_the_init_restores_the_executable_bit(tmp_path):
    """``upload_dir`` records it as object metadata; a ConfigMap could not carry modes."""
    init, = _manifest(_spec(tmp_path))["spec"]["initContainers"]
    script = init["command"][-1]
    assert "executable" in script and "chmod +x" in script


def test_the_pod_carries_the_managers_deadline_and_an_idle_pid_one(tmp_path):
    manifest = _manifest(_spec(tmp_path), deadline=930)
    spec = manifest["spec"]
    # The manager's own deadline, so the pod cannot outlive its intent even if the
    # reaper never runs — and not a hardcoded 300, which would truncate a long scenario.
    assert spec["activeDeadlineSeconds"] == 930
    assert spec["containers"][0]["command"] == ["/bin/bash", "-c", "exec sleep 930"]
    assert spec["restartPolicy"] == "Never"


def test_the_pod_mounts_no_results_volume(tmp_path):
    manifest = _manifest(_spec(tmp_path))
    mounts = [m["mountPath"] for c in manifest["spec"]["containers"]
              for m in c.get("volumeMounts", [])]
    assert "/config" in mounts
    assert "/out" not in mounts, "a diagnostic must never mount the results dir"


def test_the_pod_carries_no_configmap_volume(tmp_path):
    """The ConfigMap route is gone; a leftover volume would mean it half-survived."""
    manifest = _manifest(_spec(tmp_path, workspace=True))
    for volume in manifest["spec"]["volumes"]:
        assert "configMap" not in volume


def test_a_named_workspace_is_mounted_read_only_at_its_own_address(tmp_path):
    """Same address as the local lane, so a path from ``write_file`` works verbatim.

    Read-only in the container under test: campaign inputs are not a diagnostic's to
    rewrite. The init container mounts it writable, because it is what fills it.
    """
    manifest = _manifest(_spec(tmp_path, workspace=True))["spec"]
    main, = manifest["containers"]
    mount, = [m for m in main["volumeMounts"] if m["mountPath"] == "/sources/ws-1"]
    assert mount["readOnly"] is True
    init, = manifest["initContainers"]
    init_mount, = [m for m in init["volumeMounts"] if m["mountPath"] == "/sources/ws-1"]
    assert not init_mount.get("readOnly")
    assert "/sources/ws-1/" in init["command"][-1]


def test_no_workspace_means_no_sources_mount(tmp_path):
    """Matching the local lane, which adds the ``-v`` only when one was named."""
    manifest = _manifest(_spec(tmp_path))["spec"]
    paths = [m["mountPath"] for c in manifest["containers"] + manifest["initContainers"]
             for m in c.get("volumeMounts", [])]
    assert not any(p.startswith("/sources") for p in paths)
    assert "mystore/$S3_BUCKET/$S3_EXEC_PREFIX/workspace/" \
        not in manifest["initContainers"][0]["command"][-1]


def test_the_store_is_built_lazily_not_at_lane_construction():
    """Off-cluster, building a storage client opens a kubectl port-forward.

    The startup stray-reap constructs a lane and calls ``stop_held`` on it; if that
    forced a client, every service would open a tunnel on its first exec-adjacent call
    whether or not anything was ever staged.
    """
    built = []

    def factory():
        built.append(1)
        return _FakeStore()

    lane = KubeExecLane("ns", storage_factory=factory, bucket="b")
    assert built == [], "constructing the lane must not build the client"
    lane._require_store()
    lane._require_store()
    assert built == [1], "built once, then cached"


# -- pulling a private experiment image ---------------------------------------
#
# The exec pod runs the experiment image, which on this lane lives in the deployment's own
# registry and may be private. The scene aux pod had exactly this omission and its fix
# records why it is easy to miss: `imagePullPolicy: IfNotPresent` means a node that already
# cached the image succeeds without a credential, so the failure waits for a fresh node.


def test_the_pod_can_pull_a_private_image(tmp_path):
    spec = _manifest(_spec(tmp_path), pull_secret="robovast-registry")["spec"]
    assert spec["imagePullSecrets"] == [{"name": "robovast-registry"}]


def test_no_secret_means_no_pull_secrets_key(tmp_path):
    """A public image legitimately needs none, and an empty list is not the same as absent:
    Kubernetes rejects a nameless entry."""
    assert "imagePullSecrets" not in _manifest(_spec(tmp_path))["spec"]


def test_the_lane_passes_the_secret_it_was_built_with(tmp_path, monkeypatch):
    """The manifest is only right if the lane actually hands it over."""
    from robovast.execution.cluster_execution import kube_exec_lane as kel
    seen = {}
    monkeypatch.setattr(kel, "_pod_manifest",
                        lambda *a, **kw: seen.update(kw) or {"metadata": {"name": "p"}})
    # Patched where it is defined: start_held imports it inside the function, so replacing
    # a name on the lane's module would not reach it.
    from robovast.execution.cluster_execution import kube_client
    monkeypatch.setattr(kube_client, "wait_pod_ready", lambda *a, **kw: None)
    lane = KubeExecLane("ns", storage=_FakeStore(), bucket="b", s3_endpoint=_S3[0],
                        s3_access_key=_S3[1], s3_secret_key=_S3[2],
                        pull_secret="robovast-registry")


    class _Core:
        def create_namespaced_pod(self, namespace, manifest):
            return None

        def delete_namespaced_pod(self, *a, **kw):
            from kubernetes.client.exceptions import ApiException
            raise ApiException(status=404, reason="Not Found")

    lane._core = _Core()
    lane.start_held(_spec(tmp_path), 300)
    assert seen["pull_secret"] == "robovast-registry"


def test_the_cluster_service_gives_its_exec_lane_the_pull_secret():
    """Source-inspection, as with the kube context above: constructing a real lane needs a
    cluster. What matters is that the wiring exists at all -- it did not, and the image the
    exec pod runs is precisely the private one."""
    import inspect

    from robovast.execution.cluster_execution.cluster_service import ClusterService
    source = inspect.getsource(ClusterService._exec_lane)
    assert "pull_secret=self._registry_pull_secret()" in source


# -- holding a variation's auxiliary container --------------------------------


def _aux_held_spec(tmp_path, image="ghcr.io/example/builder"):
    from robovast.common.variation.container_runner import ContainerSpec
    return ce.ExecSpec(image=image, command="", config_dir=str(tmp_path),
                       env={}, config_name="preview-abc",
                       aux_spec=ContainerSpec(image=image, command_prefix=["build"]))


def test_a_held_aux_pod_is_the_one_an_aux_runner_knows_how_to_use(tmp_path):
    """Built by the campaign path's builder, not this lane's.

    The runner that will compose against it stages inputs into ``AUX_MOUNTABLE_PATHS`` and
    mirrors its workspace with ``mc``. A pod from ``_pod_manifest`` has neither, so it would
    come up fine and then fail at the first ``expose()`` — which is why the two manifests
    are one builder and not two.
    """
    from robovast.execution.cluster_execution.container_runner import AUX_MOUNTABLE_PATHS
    lane = _lane()
    manifest = lane._held_manifest(_aux_held_spec(tmp_path), 300, "", "qabc")

    spec = manifest["spec"]
    mounted = {m["mountPath"] for m in spec["containers"][0]["volumeMounts"]}
    assert set(AUX_MOUNTABLE_PATHS) <= mounted
    assert any(c["name"] == "mc-tools" for c in spec["initContainers"])
    # Idling on the aux spec's keep-alive, not running an entrypoint: the commands come
    # later, from the plugin.
    assert spec["containers"][0]["command"] == ["sleep", "infinity"]


def test_a_held_aux_pod_is_addressed_and_swept_like_every_other_held_one(tmp_path):
    """Its name, its container's name and its label are this lane's, whatever is inside it.

    Otherwise the probes, the execs and the post-restart stray sweep would each need to know
    which kind of pod they were looking at.
    """
    from robovast.execution.cluster_execution.kube_exec_lane import HELD_CONTAINER, _pod_name
    lane = _lane()
    manifest = lane._held_manifest(_aux_held_spec(tmp_path), 300, "", "qabc")

    assert manifest["metadata"]["name"] == _pod_name("qabc")
    assert [c["name"] for c in manifest["spec"]["containers"]] == [HELD_CONTAINER]
    key, _, value = ce.POD_LABEL.partition("=")
    assert manifest["metadata"]["labels"][key] == value


def test_holding_an_aux_container_stages_nothing(tmp_path, monkeypatch):
    """Its runner mirrors its own workspace around each command, so there is no /config
    tree to put there — and staging one would upload a directory nothing reads."""
    store = _FakeStore()
    lane = _lane(store)
    monkeypatch.setattr(lane, "stop_held", lambda slot=ce.SLOT_USER: False)
    monkeypatch.setattr(lane, "_client", lambda: _CreateRecorder())
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.kube_client.wait_pod_ready",
        lambda *a, **k: None)

    lane.start_held(_aux_held_spec(tmp_path), 300, "qabc")
    assert store.uploads == []


class _CreateRecorder:
    def __init__(self):
        self.created = []

    def create_namespaced_pod(self, namespace, manifest):
        self.created.append((namespace, manifest))


def test_a_held_exec_addresses_the_slots_pod_and_its_container(tmp_path, monkeypatch):
    """Pins the ``(pod, container)`` pair every held exec goes to.

    Nothing covered this, and a stale name in it survived a full green suite: the pair is
    built once per call and only a *live* exec would have raised. It is the address every
    other operation on a held container agrees on, so it is worth one cheap assertion.
    """
    from robovast.execution.cluster_execution.kube_exec_lane import HELD_CONTAINER, _pod_name
    lane = _lane()
    seen = {}
    monkeypatch.setattr(lane, "exec_in",
                        lambda target, argv, limit_s: seen.update(target=target) or
                        (0, "", "", False))

    lane.exec_in_held(_spec(tmp_path), 30, detach=False, slot="qabc")
    assert seen["target"] == (_pod_name("qabc"), HELD_CONTAINER)
