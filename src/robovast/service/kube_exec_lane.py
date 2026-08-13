"""The Kubernetes half of container exec: one aux pod, exec'd into.

The in-cluster counterpart of :mod:`robovast.service.docker_exec_lane`, built on the
same two primitives the aux-pod container runner already uses — a kept-alive pod and
``pods/exec``, which its docstring calls "the in-cluster equivalent of ``docker exec``".

``/config`` arrives the way a campaign Job's does: staged to the object store, then
mirrored down by an ``mc`` init container from the shared sidecar image. That is a
deliberate choice among the three transports this repo has. It replaced a ConfigMap,
which was simpler but capped the staged tree at ~900 KiB and answered "your config is
too big" with "run it as a campaign instead" — the exact cost this tool exists to avoid.
It is not the aux pod's tar-over-``pods/exec`` either, because that needs the pod to be
*running* before its files exist, and needs ``tar``/``base64`` in an image we do not
control; ``mc`` is already a hard requirement of every cluster experiment image and the
sidecar carries its own.

The deeper reason is that a diagnostic should not have its own staging path. Whatever a
run does to get files into a container, this must do too, or the check can pass on a
config the run would fail to stage — or fail on one it would not.

Staging fits an init container because it happens exactly once per pod:
``ContainerExecManager`` discards a redundant staging when it reuses a held pod, and
replaces the pod outright when the identity changes, so ``/config`` never needs
refreshing underneath a live pod.

Three things here exist because a live cluster disagreed with what looked obviously
correct, and each was invisible on the local lane:

- the kube **context** must come from the service, or this execs into whichever cluster
  the kubeconfig currently points at while looking perfectly valid;
- :meth:`KubeExecLane.stop_held` **waits** for deletion, because a Kubernetes delete
  returns while the pod is still ``Terminating`` and the next start then collides with
  the corpse;
- the "is anything running?" probe uses **shell builtins only**, since a probe that
  spawns ``ls``/``wc`` counts its own helpers and reports an idle pod as busy forever.
"""

import logging

from robovast.service.container_exec import CONTAINER_NAME, POD_LABEL, ExecSpec

logger = logging.getLogger(__name__)

_CONTAINER = "exec"
_PROBE_TIMEOUT_S = 30

#: Key prefix every staged exec tree lives under, inside the lane's bucket.
EXEC_PREFIX = "container-exec"

#: Where the workspace is mirrored, when one was named. The local lane bind-mounts it at
#: the same address, so a path taken from ``write_file`` is usable verbatim on both.
SOURCES_ROOT = "/sources"


def _pod_name() -> str:
    # Same name as the local container, so the "at most one" rule reads identically on
    # both lanes and a stray is found the same way.
    return CONTAINER_NAME


def exec_prefix(namespace: str) -> str:
    """Where this namespace's exec tree is staged.

    Namespaced because the pod name is fixed and therefore unique *per namespace*, while
    a shared-bucket deployment gives several namespaces one bucket. One definition,
    because staging, the init container's mirror and the cleanup must address the same
    keys.
    """
    return f"{EXEC_PREFIX}/{namespace}"


class KubeExecLane:
    """Runs exec commands in a single aux pod, staged from the object store."""

    def __init__(self, namespace: str, owner_ref: dict | None = None,
                 kube_context: str | None = None, *, storage=None,
                 storage_factory=None, bucket: str = "", s3_endpoint: str = "",
                 s3_access_key: str = "", s3_secret_key: str = ""):
        self._namespace = namespace
        self._owner_ref = owner_ref
        # Must be the service's own context: without it this would load the kubeconfig's
        # *current* context and exec into a different cluster than the one the campaigns
        # run on — answering a question about somewhere else entirely.
        self._kube_context = kube_context
        self._core = None
        # The service-side storage client (which may reach the store through a
        # port-forward) and the in-cluster endpoint the pod itself must use. They are
        # deliberately separate: an off-cluster service talks to 127.0.0.1:<port>, while
        # the pod talks to the cluster Service.
        #
        # A factory rather than a client, because building one off-cluster opens a
        # kubectl port-forward: a lane that is constructed and never staged into — the
        # throwaway one the startup stray-reap builds, say — should not pay for a tunnel
        # it will not use.
        self._storage = storage
        self._storage_factory = storage_factory
        self._bucket = bucket
        self._s3 = (s3_endpoint, s3_access_key, s3_secret_key)

    @property
    def _has_store(self) -> bool:
        return bool(self._bucket) and (self._storage is not None
                                       or self._storage_factory is not None)

    def _client(self):
        if self._core is None:
            from kubernetes import client

            from robovast.common.kube import load_kube_config
            load_kube_config(context=self._kube_context)
            self._core = client.CoreV1Api()
        return self._core

    def _require_store(self):
        """The staging store, or a refusal naming what is missing.

        Staging has no fallback on purpose. Quietly running the command against an
        unstaged ``/config`` would answer a different question than the caller asked and
        look like a pass.
        """
        if not self._has_store:
            raise RuntimeError(
                "container exec on the cluster lane stages /config through the object "
                "store, and this lane was built without one. That is a service "
                "configuration problem, not something a command can work around.")
        if self._storage is None:
            self._storage = self._storage_factory()
        return self._storage

    # -- ExecLane ---------------------------------------------------------

    def run_once(self, spec: ExecSpec, limit_s: int) -> tuple[int, str, str, bool]:
        """No throwaway-pod path: create, exec, delete — the pod *is* the container.

        A one-shot on this lane is a held pod that is torn down immediately, because a
        pod cannot both run a command and hand back its output the way ``docker run``
        does without polling logs to completion.
        """
        self.start_held(spec, limit_s)
        try:
            return self.exec_in_held(spec, limit_s, detach=False)
        finally:
            self.stop_held()

    def start_held(self, spec: ExecSpec, deadline_s: int) -> None:
        from kubernetes.client.rest import ApiException

        from robovast.common.kube import wait_pod_ready
        core = self._client()
        self.stop_held()
        prefix = self._stage(spec)
        try:
            core.create_namespaced_pod(
                self._namespace,
                _pod_manifest(spec, deadline_s, self._namespace, self._owner_ref,
                              self._s3, self._bucket, prefix))
        except ApiException as e:
            self._discard_staged()
            raise RuntimeError(f"could not start exec pod: {e.reason}") from e
        try:
            wait_pod_ready(core, self._namespace, _pod_name())
        except BaseException:
            # A pod that never came up still holds a staged tree and a pod object; the
            # caller sees an exception and will not call stop_held itself.
            self.stop_held()
            raise

    def _stage(self, spec: ExecSpec) -> str:
        """Upload ``/config`` (and the workspace, if any) and return the key prefix.

        ``upload_dir`` tags executables with ``x-amz-meta-executable``, which the init
        container reads back — so a staged run file keeps its mode. The ConfigMap this
        replaced could not carry modes at all.
        """
        storage = self._require_store()
        prefix = exec_prefix(self._namespace)
        storage.upload_dir(spec.config_dir, self._bucket, f"{prefix}/config")
        if spec.workspace_dir and spec.workspace_id:
            storage.upload_dir(spec.workspace_dir, self._bucket, f"{prefix}/workspace")
        return prefix

    def _discard_staged(self) -> int:
        """Delete this namespace's staged tree. Best-effort, but noisy when it fails.

        Cleanup must not turn a successful stop into an error — but a leaked prefix is
        the one thing nothing else reaps, so silence would be worse.
        """
        if not self._has_store:
            return 0
        try:
            return self._require_store().delete_prefix(self._bucket,
                                                       exec_prefix(self._namespace))
        except Exception as e:  # noqa: BLE001 - cleanup never fails a stop
            logger.warning("could not discard the staged exec tree: %s", e)
            return 0

    def exec_in_held(self, spec: ExecSpec, limit_s: int,
                     detach: bool) -> tuple[int, str, str, bool]:
        from robovast.common.kube import exec_stream
        # Both forms come from the spec, so the liveness check a detached start needs
        # cannot be present on one lane and missing on the other — which is exactly how
        # it was, until a scenario silently failed to start.
        if detach:
            argv = ["/bin/bash", "-c", spec.detached_start_script()]
        else:
            argv = spec.foreground_argv()
        # The env is baked into the pod at creation, so it is not re-sent per exec —
        # unlike docker exec, where each call carries it.
        return exec_stream(self._client(), _pod_name(), self._namespace, _CONTAINER,
                           argv, limit_s=limit_s)

    def stop_held(self) -> bool:
        """Delete the pod, **wait until it is gone**, and drop the staged tree.

        The wait is the whole point: a Kubernetes delete returns while the pod is still
        ``Terminating``, so a caller that immediately started another one got
        ``AlreadyExists``. The local lane's ``docker rm -f`` is synchronous, and this must
        offer the same contract — "stopped" has to mean stopped, or the single-container
        rule cannot be relied on.
        """
        from kubernetes.client.rest import ApiException

        from robovast.common.kube import wait_pod_gone
        core = self._client()
        existed = False
        try:
            # A diagnostic pod has nothing to flush, so it does not need the default
            # grace period.
            core.delete_namespaced_pod(_pod_name(), self._namespace,
                                       grace_period_seconds=0)
            existed = True
        except ApiException as e:
            if e.status != 404:
                logger.warning("deleting %s failed: %s", _pod_name(), e.reason)
        if existed:
            wait_pod_gone(core, self._namespace, _pod_name())
        # Unconditional: a previous process may have left a tree with no pod beside it,
        # and this is the only thing that reaps it.
        self._discard_staged()
        return existed

    #: Counts processes that are neither the idle PID 1 nor the probe itself, using only
    #: shell builtins. Spawning anything — ``ls``, ``wc``, ``ps`` — would count the
    #: probe's own helpers: the first version of this piped ``ls`` into ``wc`` and read 4
    #: processes in an *idle* pod, so it reported "busy" forever and no container was
    #: ever idle-reaped. A glob plus arithmetic spawns nothing, so idle reads exactly 0.
    _PROCESS_COUNT_SH = (
        'n=0; for p in /proc/[0-9]*; do pid=${p#/proc/}; '
        '[ "$pid" = 1 ] && continue; '
        '[ "$pid" = "$$" ] && continue; '
        '[ "$pid" = "$PPID" ] && continue; '
        'n=$((n+1)); done; echo $n')

    def held_workload_running(self) -> bool:
        """True if anything besides the idle PID 1 runs in the pod.

        Same rule as the local lane, asked through ``pods/exec`` since there is no
        ``docker top`` here. A failure other than "no such pod" propagates, so an
        unanswerable probe is never read as "idle".
        """
        from kubernetes.client.rest import ApiException

        from robovast.common.kube import exec_stream
        try:
            _code, out, _err, _timed_out = exec_stream(
                self._client(), _pod_name(), self._namespace, _CONTAINER,
                ["/bin/sh", "-c", self._PROCESS_COUNT_SH],
                limit_s=_PROBE_TIMEOUT_S)
        except ApiException as e:
            if e.status == 404:
                return False
            raise
        try:
            count = int((out or "0").strip().splitlines()[-1])
        except (ValueError, IndexError) as exc:
            raise RuntimeError(
                f"could not read process count from {_pod_name()}") from exc
        return count > 0


def _labels() -> dict:
    key, _, value = POD_LABEL.partition("=")
    return {key: value}


def _mirror_command(spec: ExecSpec) -> str:
    """The init container's ``mc`` script: mirror the staged tree, restore exec bits.

    Modelled on the campaign job's init (``kubernetes_backend``) and the build context's
    (``cluster_image_build.context_fetch_command``) — same alias, same prefix-per-mount
    shape, so all three read alike.
    """
    parts = [
        'mc alias set mystore "$S3_ENDPOINT" "$S3_ACCESS_KEY" "$S3_SECRET_KEY"',
        'mc mirror "mystore/$S3_BUCKET/$S3_EXEC_PREFIX/config/" /config/',
    ]
    if spec.workspace_dir and spec.workspace_id:
        parts.append(
            'mc mirror "mystore/$S3_BUCKET/$S3_EXEC_PREFIX/workspace/" '
            f'{SOURCES_ROOT}/{spec.workspace_id}/')
    # Restore the executable bit from the object metadata upload_dir wrote. Trailing
    # `true` so a tree with no executables in it does not fail the init container.
    restore = (
        'src="mystore/$S3_BUCKET/$S3_EXEC_PREFIX/config/"; '
        'mc find "$src" 2>/dev/null | while IFS= read -r obj; do '
        "mc stat --json \"$obj\" 2>/dev/null | grep -qi 'executable.*yes' && "
        'chmod +x "/config/${obj#$src}" || true; done; true')
    return " && ".join(parts) + "; " + restore


def _pod_manifest(spec: ExecSpec, deadline_s: int, namespace: str,
                  owner_ref: dict | None, s3: tuple, bucket: str,
                  prefix: str) -> dict:
    """A single kept-alive container with ``/config`` mirrored down by an init container.

    ``activeDeadlineSeconds`` is the manager's own deadline, so the pod cannot outlive
    the service's intent even if the reaper never runs.
    """
    from robovast.execution.cluster_execution.cluster_image_build import s3_init_env
    from robovast.common.execution import resolve_sidecar_image

    metadata = {"name": _pod_name(), "namespace": namespace, "labels": dict(_labels())}
    if owner_ref:
        metadata["ownerReferences"] = [owner_ref]
    endpoint, access_key, secret_key = s3
    init_env = s3_init_env(endpoint, access_key, secret_key, bucket, prefix,
                           prefix_var="S3_EXEC_PREFIX")

    volumes = [{"name": "config", "emptyDir": {}}]
    init_mounts = [{"name": "config", "mountPath": "/config"}]
    main_mounts = [{"name": "config", "mountPath": "/config"}]
    if spec.workspace_dir and spec.workspace_id:
        mount_path = f"{SOURCES_ROOT}/{spec.workspace_id}"
        volumes.append({"name": "sources", "emptyDir": {}})
        init_mounts.append({"name": "sources", "mountPath": mount_path})
        # Read-only in the main container: campaign inputs are not a diagnostic's to
        # rewrite, matching the local lane's `-v <dir>:/sources/<id>:ro`.
        main_mounts.append({"name": "sources", "mountPath": mount_path,
                            "readOnly": True})

    env = [{"name": k, "value": str(v)} for k, v in spec.env.items()]
    return {
        "apiVersion": "v1", "kind": "Pod", "metadata": metadata,
        "spec": {
            "restartPolicy": "Never",
            "activeDeadlineSeconds": int(deadline_s),
            "initContainers": [{
                # The sidecar, not the experiment image: it carries `mc`, and staging
                # must not depend on what the image under test happens to install.
                "name": "s3-init", "image": resolve_sidecar_image(),
                "imagePullPolicy": "IfNotPresent",
                "command": ["sh", "-c", _mirror_command(spec)],
                "env": init_env,
                "volumeMounts": init_mounts,
            }],
            "containers": [{
                "name": _CONTAINER, "image": spec.image,
                "imagePullPolicy": "IfNotPresent",
                # Idle PID 1, so exec'd commands run against a stable container and
                # anything backgrounded has something to reparent to.
                "command": ["/bin/bash", "-c", f"exec sleep {int(deadline_s)}"],
                "env": env,
                "volumeMounts": main_mounts,
            }],
            "volumes": volumes,
        },
    }
