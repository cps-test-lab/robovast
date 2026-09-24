"""The Kubernetes half of container exec: one aux pod, exec'd into.

The :class:`~robovast.service.container_exec.ExecLane` of the cluster lane, built on the
same two primitives the aux-pod container runner already uses — a kept-alive pod and
``pods/exec``, which its docstring calls "the in-cluster equivalent of ``docker exec``".

``/config`` arrives the way a campaign Job's does: staged by the service on its own disk,
then fetched as one tar stream from the data plane by an init container from the shared
sidecar image (:mod:`.pod_access`). That is a deliberate choice among the transports this
repo has. A ConfigMap would be simpler but caps the staged tree at ~900 KiB and answers
"your config is too big" with "run it as a campaign instead" — the exact cost this tool
exists to avoid. The aux pod's exec-channel transfer is not it either, because that needs
the pod to be *running* before its files exist, and needs tools in an image we do not
control; the sidecar carries its own.

The deeper reason is that a diagnostic should not have its own staging path. Whatever a
run does to get files into a container, this must do too, or the check can pass on a
config the run would fail to stage — or fail on one it would not.

Staging fits an init container because it happens exactly once per pod:
``ContainerExecManager`` discards a redundant staging when it reuses a held pod, and
replaces the pod outright when the identity changes, so ``/config`` never needs
refreshing underneath a live pod.

Three things here exist because a live cluster disagreed with what looked obviously
correct:

- the kube **context** must come from the service, or this execs into whichever cluster
  the kubeconfig currently points at while looking perfectly valid;
- :meth:`KubeExecLane.stop_held` **waits** for deletion, because a Kubernetes delete
  returns while the pod is still ``Terminating`` and the next start then collides with
  the corpse;
- the "is anything running?" probe uses **shell builtins only**, since a probe that
  spawns ``ls``/``wc`` counts its own helpers and reports an idle pod as busy forever.
"""

import logging
import shutil
from pathlib import Path

from robovast.service.container_exec import SLOT_USER, POD_LABEL, ExecSpec, container_name

from . import pod_access

logger = logging.getLogger(__name__)

#: The single container inside a held pod. Public because the pod name and this name
#: are one address, and a caller that is handed the pod (an aux runner exec'ing into a
#: held helper image) has to name the container too.
HELD_CONTAINER = "exec"
_PROBE_TIMEOUT_S = 30

#: The staged slot every exec tree lives under, on the service's disk.
EXEC_PREFIX = "container-exec"

#: The two subtrees of a staged exec slot: what lands at ``/config``, and the workspace.
CONFIG_SUBDIR = "config"
WORKSPACE_SUBDIR = "workspace"

#: Where the workspace is fetched to, when one was named: the address ``write_file``
#: uses, so a path taken from there is usable verbatim.
SOURCES_ROOT = "/sources"


def _pod_name(slot: str = SLOT_USER) -> str:
    # Same name as the local container, so a slot's container reads identically on both
    # lanes and a stray is found the same way. Every slot keeps POD_LABEL, so the stray
    # sweep finds a query pod too — its slot key is derived from an identity no restarted
    # service still remembers.
    return container_name(slot)


def exec_slot(namespace: str, slot: str = SLOT_USER) -> str:
    """The staged slot holding this namespace's exec tree for *slot*.

    Namespaced because a pod name is unique only *per namespace*, while one service may
    serve several. Per slot for the same reason one step down: several pods are held at
    once, and a shared slot would have each one's ``start_held`` overwrite the tree the
    others already fetched. One definition, because staging, the init container's fetch,
    the token's scope and the cleanup must address the same slot.
    """
    return f"{EXEC_PREFIX}/{namespace}/{slot}"


class KubeExecLane:
    """Runs exec commands in a single aux pod, staged through the service's data plane.

    *stage_dir*, *discard_staged* and *token_for* are the service's own
    ``staged_dir(slot)``, ``discard_staged(slot)`` and ``scoped_token(scope)``: the lane
    writes the tree the pod will fetch, mints the token that reaches it, and drops it
    with the pod. All three are required -- a lane that cannot stage would answer a
    different question than the caller asked, against an unstaged ``/config``, and look
    like a pass.
    """

    def __init__(self, namespace: str, owner_ref: dict | None = None,
                 kube_context: str | None = None, *, stage_dir, discard_staged,
                 token_for, pull_secret: str = ""):
        self._namespace = namespace
        self._owner_ref = owner_ref
        # The image under test is one of ours, from this deployment's registry, and it may
        # be private. Empty is legitimate (a public image, or no registry configured).
        self._pull_secret = pull_secret
        # Must be the service's own context: without it this would load the kubeconfig's
        # *current* context and exec into a different cluster than the one the campaigns
        # run on — answering a question about somewhere else entirely.
        self._kube_context = kube_context
        self._core = None
        self._stage_dir = stage_dir
        self._discard = discard_staged
        self._token_for = token_for

    def _client(self):
        if self._core is None:
            from .kube_client import core_v1_client
            self._core = core_v1_client(self._kube_context)
        return self._core

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

    # -- ExecLane, continued ----------------------------------------------

    def start_held(self, spec: ExecSpec, deadline_s: int,
                   slot: str = SLOT_USER) -> None:
        from kubernetes.client.rest import ApiException

        from .kube_client import raise_api_error, wait_pod_ready
        core = self._client()
        self.stop_held(slot)
        # An aux container stages nothing: its runner moves its own workspace through the
        # data plane around each command, so there is no /config tree to put here.
        if spec.aux_spec is None:
            self._stage(spec, slot)
        try:
            core.create_namespaced_pod(
                self._namespace, self._held_manifest(spec, deadline_s, slot))
        except ApiException as e:
            self._discard_staged(slot)
            raise_api_error(e, "could not start exec pod")
        try:
            wait_pod_ready(core, self._namespace, _pod_name(slot))
        except BaseException:
            # A pod that never came up still holds a staged tree and a pod object; the
            # caller sees an exception and will not call stop_held itself.
            self.stop_held(slot)
            raise

    def _held_manifest(self, spec: ExecSpec, deadline_s: int, slot: str) -> dict:
        """The pod for *slot*: an experiment container, or a variation's helper image.

        The aux form comes from ``build_aux_pod_manifest`` — the campaign path's builder —
        rather than from :func:`_pod_manifest`, so the pod is the one an aux runner already
        knows how to use: the transfer container that moves its workspace, and an emptyDir
        at each of ``AUX_MOUNTABLE_PATHS`` that ``expose()`` stages into. Only the pod's
        name, its single container's name and its label are this lane's, so every held pod
        is addressed, probed and swept identically whatever is inside it. Reusing that
        builder is also what keeps the data-plane wiring and the pull secret in one place
        instead of two.
        """
        if spec.aux_spec is None:
            token = self._token_for(pod_access.staged_scope(exec_slot(self._namespace, slot)))
            return _pod_manifest(spec, deadline_s, self._namespace, self._owner_ref,
                                 token, pull_secret=self._pull_secret, slot=slot)

        from .container_runner import build_aux_pod_manifest
        aux = spec.aux_spec
        return build_aux_pod_manifest(
            slot, [aux], self._namespace, owner_ref=self._owner_ref,
            stage_dir=self._stage_dir, token_for=self._token_for,
            deadline_seconds=deadline_s, pull_secret=self._pull_secret,
            pod_name=_pod_name(slot),
            container_names={aux.container_name(): HELD_CONTAINER},
            extra_labels=_labels())

    def _stage(self, spec: ExecSpec, slot: str = SLOT_USER) -> Path:
        """Write ``/config`` (and the workspace, if any) into the slot's staged tree.

        A plain copy: the tar the pod fetches carries every mode, so a staged run file
        keeps its executable bit with nothing to restore. Returns the tree's root.
        """
        name = exec_slot(self._namespace, slot)
        # The tree a previous hold left would otherwise be fetched as part of this one.
        self._discard(name)
        root = Path(self._stage_dir(name))
        shutil.copytree(spec.config_dir, root / CONFIG_SUBDIR)
        if spec.workspace_dir and spec.workspace_id:
            shutil.copytree(spec.workspace_dir, root / WORKSPACE_SUBDIR)
        return root

    def _discard_staged(self, slot: str = SLOT_USER) -> bool:
        """Delete the slot's staged tree. Best-effort, but noisy when it fails.

        Cleanup must not turn a successful stop into an error — but a leaked tree is
        the one thing nothing else reaps, so silence would be worse.
        """
        try:
            return bool(self._discard(exec_slot(self._namespace, slot)))
        except Exception as e:  # noqa: BLE001 - cleanup never fails a stop
            logger.warning("could not discard the staged exec tree: %s", e)
            return False

    def exec_in(self, target, argv: list, limit_s: int,
                env: dict | None = None) -> tuple[int, str, str, bool]:
        """Exec into *target*, a ``(pod, container)`` pair.

        *env* is accepted for one signature across lanes and ignored here: a pod bakes its
        environment at creation, so there is nothing per-exec to carry — unlike ``docker
        exec``, where each call carries it.
        """
        from .kube_client import exec_stream
        pod, container = target
        return exec_stream(pod, self._namespace, container,
                           list(argv), limit_s=limit_s)

    def exec_in_held(self, spec: ExecSpec, limit_s: int, detach: bool,
                     slot: str = SLOT_USER) -> tuple[int, str, str, bool]:
        # Both forms come from the spec, so the liveness check a detached start needs
        # cannot be present on one lane and missing on the other — which is exactly how
        # it was, until a scenario silently failed to start.
        if detach:
            argv = ["/bin/bash", "-c", spec.detached_start_script()]
        else:
            argv = spec.foreground_argv()
        return self.exec_in((_pod_name(slot), HELD_CONTAINER), argv, limit_s)

    def stop_held(self, slot: str = SLOT_USER) -> bool:
        """Delete the pod, **wait until it is gone**, and drop the staged tree.

        The wait is the whole point: a Kubernetes delete returns while the pod is still
        ``Terminating``, so a caller that immediately started another one got
        ``AlreadyExists``. "Stopped" has to mean stopped, or the single-container rule
        cannot be relied on.
        """
        from kubernetes.client.rest import ApiException

        from .kube_client import api_error_reason, wait_pod_gone
        core = self._client()
        pod = _pod_name(slot)
        existed = False
        try:
            # A diagnostic pod has nothing to flush, so it does not need the default
            # grace period.
            core.delete_namespaced_pod(pod, self._namespace, grace_period_seconds=0)
            existed = True
        except ApiException as e:
            if e.status != 404:
                logger.warning("deleting %s failed: %s", pod, api_error_reason(e))
        if existed:
            wait_pod_gone(core, self._namespace, pod)
        # Unconditional: a previous process may have left a tree with no pod beside it,
        # and this is the only thing that reaps it.
        self._discard_staged(slot)
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

    def sweep_held(self) -> list:
        """Delete every exec pod this lane owns, and the trees they staged.

        See :func:`_sweep_held_pods`: after a restart the query slots' keys are gone, so
        the label is the only handle left on the pods they made.
        """
        deleted = _sweep_held_pods(self)
        # The staged trees are keyed by slot too, and the same restart lost those keys.
        # One discard of the namespace's tree covers every slot in it.
        try:
            self._discard(exec_slot(self._namespace, "").rstrip("/"))
        except Exception as e:  # noqa: BLE001 - cleanup never fails startup
            logger.warning("could not discard staged exec trees: %s", e)
        return deleted

    def held_container_alive(self, slot: str = SLOT_USER) -> bool:
        """True while *slot*'s pod is running -- read rather than exec'd into.

        A read, because this is the question asked *before* the pod is trusted enough to
        exec into: asked through an exec it would fail for the very condition it exists to
        detect, and report that failure as the deployment's rather than this pod's.
        """
        from kubernetes.client.rest import ApiException

        from .kube_client import raise_api_error
        pod_name = _pod_name(slot)
        try:
            pod = self._client().read_namespaced_pod(pod_name, self._namespace)
        except ApiException as e:
            if e.status == 404:
                return False
            raise_api_error(e, f"could not read {pod_name} to see whether it is still up")
        if (getattr(pod.status, "phase", "") or "") != "Running":
            return False
        for status in (pod.status.container_statuses or []):
            if status.name == HELD_CONTAINER:
                return bool(getattr(status.state, "running", None))
        return False

    def held_workload_running(self, slot: str = SLOT_USER) -> bool:
        """True if anything besides the idle PID 1 runs in the pod.

        Asked through ``pods/exec``, since there is no ``docker top`` here. A failure other than "no such pod" propagates, so an
        unanswerable probe is never read as "idle".
        """
        from kubernetes.client.rest import ApiException

        from .kube_client import exec_stream
        pod = _pod_name(slot)
        try:
            _code, out, _err, _timed_out = exec_stream(
                pod, self._namespace, HELD_CONTAINER,
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
                f"could not read process count from {pod}") from exc
        return count > 0


def _sweep_held_pods(lane) -> list:
    """Delete every exec pod in the namespace, by label. Returns the names deleted.

    By label and not by name because a query pod's name carries a hash of the identity it
    was started for, and nothing persists those across a service restart — so after one,
    the label is the only thing that can still find it.
    """
    from kubernetes.client.rest import ApiException

    from .kube_client import api_error_reason, wait_pod_gone
    core = lane._client()  # noqa: SLF001 - the lane's own helper, called from its module
    try:
        found = core.list_namespaced_pod(lane._namespace,  # noqa: SLF001
                                         label_selector=POD_LABEL)
    except ApiException as e:
        logger.warning("could not list stray exec pods: %s", api_error_reason(e))
        return []
    deleted = []
    for pod in found.items:
        name = pod.metadata.name
        try:
            core.delete_namespaced_pod(name, lane._namespace,  # noqa: SLF001
                                       grace_period_seconds=0)
        except ApiException as e:
            if e.status != 404:
                logger.warning("deleting %s failed: %s", name, api_error_reason(e))
            continue
        wait_pod_gone(core, lane._namespace, name)  # noqa: SLF001
        deleted.append(name)
    return deleted


def _labels() -> dict:
    key, _, value = POD_LABEL.partition("=")
    return {key: value}


def _fetch_command(spec: ExecSpec, namespace: str, slot: str) -> str:
    """The init container's shell: fetch ``/config``, and the workspace when one was named.

    Two fetches of one slot, each a subtree of it (the ``path`` query), so the tree the
    service staged lands split across the two mounts exactly as it was staged. Modes ride
    in the tar, so there is nothing to restore afterwards.
    """
    route = f"/staged/{exec_slot(namespace, slot)}"
    parts = [pod_access.fetch_command(route, "/config", f"path={CONFIG_SUBDIR}")]
    if spec.workspace_dir and spec.workspace_id:
        parts.append(pod_access.fetch_command(
            route, f"{SOURCES_ROOT}/{spec.workspace_id}", f"path={WORKSPACE_SUBDIR}"))
    return " && ".join(parts)


def _pod_manifest(spec: ExecSpec, deadline_s: int, namespace: str,
                  owner_ref: dict | None, token: str, pull_secret: str = "",
                  slot: str = SLOT_USER) -> dict:
    """A single kept-alive container with ``/config`` fetched by an init container.

    ``activeDeadlineSeconds`` is the manager's own deadline, so the pod cannot outlive
    the service's intent even if the reaper never runs.

    *token* reaches this slot's staged tree and nothing else; it rides in the init
    container's env (:func:`pod_access.staged_pod_env`) and nowhere the image under test
    can read it.

    *pull_secret* authenticates the pull of the experiment image. It covers the whole pod
    because that is the only granularity Kubernetes offers, but only the main container
    needs it: the init container is the public sidecar.
    """
    from robovast.common.execution import resolve_sidecar_image

    from .kubernetes_backend import pull_policy_for

    metadata = {"name": _pod_name(slot), "namespace": namespace,
                "labels": dict(_labels())}
    if owner_ref:
        metadata["ownerReferences"] = [owner_ref]

    volumes = [{"name": "config", "emptyDir": {}}]
    init_mounts = [{"name": "config", "mountPath": "/config"}]
    main_mounts = [{"name": "config", "mountPath": "/config"}]
    if spec.workspace_dir and spec.workspace_id:
        mount_path = f"{SOURCES_ROOT}/{spec.workspace_id}"
        volumes.append({"name": "sources", "emptyDir": {}})
        init_mounts.append({"name": "sources", "mountPath": mount_path})
        # Read-only in the main container: campaign inputs are not a diagnostic's to
        # rewrite.
        main_mounts.append({"name": "sources", "mountPath": mount_path,
                            "readOnly": True})

    env = [{"name": k, "value": str(v)} for k, v in spec.env.items()]
    return {
        "apiVersion": "v1", "kind": "Pod", "metadata": metadata,
        "spec": {
            "restartPolicy": "Never",
            "activeDeadlineSeconds": int(deadline_s),
            "initContainers": [{
                # The sidecar, not the experiment image: it carries the transfer tools,
                # and staging must not depend on what the image under test installs.
                "name": "staged-fetch", "image": resolve_sidecar_image(),
                "imagePullPolicy": "IfNotPresent",
                "command": ["sh", "-c", _fetch_command(spec, namespace, slot)],
                "env": pod_access.staged_pod_env(namespace, token),
                "volumeMounts": init_mounts,
            }],
            "containers": [{
                "name": HELD_CONTAINER, "image": spec.image,
                # Resolved from the ref, exactly as a run's pods resolve it: a digest names
                # its bytes and cannot go stale, a tag can be re-pushed under us. A held
                # container answers the checks people run to see whether a republished image
                # is good, so a hard-coded `IfNotPresent` here answers them from the node's
                # cached copy with no way to tell.
                "imagePullPolicy": pull_policy_for(spec.image),
                # Idle PID 1, so exec'd commands run against a stable container and
                # anything backgrounded has something to reparent to.
                "command": ["/bin/bash", "-c", f"exec sleep {int(deadline_s)}"],
                "env": env,
                "volumeMounts": main_mounts,
            }],
            "volumes": volumes,
            **({"imagePullSecrets": [{"name": pull_secret}]} if pull_secret else {}),
        },
    }
