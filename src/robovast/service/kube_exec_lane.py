"""The Kubernetes half of container exec: one aux pod, exec'd into.

The in-cluster counterpart of :mod:`robovast.service.docker_exec_lane`, built on the
same two primitives the aux-pod container runner already uses — a kept-alive pod and
``pods/exec``, which its docstring calls "the in-cluster equivalent of ``docker exec``".

``/config`` arrives as a ConfigMap rather than a bind mount, the way
:mod:`robovast.execution.cluster_execution.postprocess_job` already gets scripts into a
one-off Job. That bounds what this lane can stage: a ConfigMap holds ~1 MiB, which is
ample for an entrypoint, a scenario and its parameters, and is checked rather than
allowed to fail as an opaque API error.

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
import os

from robovast.service.container_exec import CONTAINER_NAME, POD_LABEL, ExecSpec

logger = logging.getLogger(__name__)

#: A ConfigMap's practical ceiling (1 MiB of etcd value). Staged trees are far smaller,
#: so exceeding it means something unexpected is being staged — say so, rather than let
#: the API reject it with a message that does not name the cause.
_CONFIGMAP_LIMIT_BYTES = 900 * 1024

_CONTAINER = "exec"
_PROBE_TIMEOUT_S = 30


def _pod_name() -> str:
    # Same name as the local container, so the "at most one" rule reads identically on
    # both lanes and a stray is found the same way.
    return CONTAINER_NAME


class KubeExecLane:
    """Runs exec commands in a single aux pod."""

    def __init__(self, namespace: str, owner_ref: dict | None = None,
                 kube_context: str | None = None):
        self._namespace = namespace
        self._owner_ref = owner_ref
        # Must be the service's own context: without it this would load the kubeconfig's
        # *current* context and exec into a different cluster than the one the campaigns
        # run on — answering a question about somewhere else entirely.
        self._kube_context = kube_context
        self._core = None

    def _client(self):
        if self._core is None:
            from kubernetes import client

            from robovast.common.kube import load_kube_config
            load_kube_config(context=self._kube_context)
            self._core = client.CoreV1Api()
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

    def start_held(self, spec: ExecSpec, deadline_s: int) -> None:
        from kubernetes.client.rest import ApiException
        core = self._client()
        self.stop_held()
        data = _config_payload(spec.config_dir)
        try:
            core.create_namespaced_config_map(
                self._namespace, _configmap_manifest(data, self._namespace,
                                                     self._owner_ref))
            core.create_namespaced_pod(
                self._namespace,
                _pod_manifest(spec, deadline_s, self._namespace, self._owner_ref))
        except ApiException as e:
            raise RuntimeError(f"could not start exec pod: {e.reason}") from e
        _wait_ready(core, self._namespace, _pod_name())

    def exec_in_held(self, spec: ExecSpec, limit_s: int,
                     detach: bool) -> tuple[int, str, str, bool]:
        from kubernetes.stream import stream
        # Both forms come from the spec, so the liveness check a detached start needs
        # cannot be present on one lane and missing on the other — which is exactly how
        # it was, until a scenario silently failed to start.
        if detach:
            argv = ["/bin/bash", "-c", spec.detached_start_script()]
        else:
            argv = spec.foreground_argv()
        # The env is baked into the pod at creation, so it is not re-sent per exec —
        # unlike docker exec, where each call carries it.
        resp = stream(self._client().connect_get_namespaced_pod_exec,
                      _pod_name(), self._namespace, container=_CONTAINER,
                      command=argv, stderr=True, stdin=False, stdout=True,
                      tty=False, _preload_content=False)
        out, err = [], []
        timed_out = False
        try:
            while resp.is_open():
                resp.update(timeout=max(1, int(limit_s)))
                if resp.peek_stdout():
                    out.append(resp.read_stdout())
                if resp.peek_stderr():
                    err.append(resp.read_stderr())
                if not resp.is_open():
                    break
            code = resp.returncode
        finally:
            resp.close()
        if code is None:
            timed_out, code = True, 124
        return code, "".join(out), "".join(err), timed_out

    def stop_held(self) -> bool:
        """Delete the pod and its ConfigMap, and **wait until they are gone**.

        The wait is the whole point: a Kubernetes delete returns while the pod is still
        ``Terminating``, so a caller that immediately started another one got
        ``AlreadyExists``. The local lane's ``docker rm -f`` is synchronous, and this must
        offer the same contract — "stopped" has to mean stopped, or the single-container
        rule cannot be relied on.
        """
        from kubernetes.client.rest import ApiException
        core = self._client()
        existed = False
        for delete, name in ((core.delete_namespaced_pod, _pod_name()),
                             (core.delete_namespaced_config_map, _pod_name())):
            try:
                # A diagnostic pod has nothing to flush, so it does not need the default
                # grace period.
                kwargs = {"grace_period_seconds": 0} if "pod" in delete.__name__ else {}
                delete(name, self._namespace, **kwargs)
                existed = True
            except ApiException as e:
                if e.status != 404:
                    logger.warning("deleting %s failed: %s", name, e.reason)
        if existed:
            self._wait_gone(core)
        return existed

    def _wait_gone(self, core, timeout_s: float = 120.0) -> None:
        import time
        from kubernetes.client.rest import ApiException
        deadline = time.monotonic() + timeout_s
        for read in (core.read_namespaced_pod, core.read_namespaced_config_map):
            while time.monotonic() < deadline:
                try:
                    read(_pod_name(), self._namespace)
                except ApiException as e:
                    if e.status == 404:
                        break
                    raise
                time.sleep(1)
            else:
                raise RuntimeError(
                    f"{_pod_name()} did not finish terminating within {int(timeout_s)}s")

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
        from kubernetes.stream import stream
        try:
            resp = stream(self._client().connect_get_namespaced_pod_exec,
                          _pod_name(), self._namespace, container=_CONTAINER,
                          command=["/bin/sh", "-c", self._PROCESS_COUNT_SH],
                          stderr=False, stdin=False, stdout=True, tty=False)
        except ApiException as e:
            if e.status == 404:
                return False
            raise
        try:
            count = int((resp or "0").strip().splitlines()[-1])
        except (ValueError, IndexError) as exc:
            raise RuntimeError(
                f"could not read process count from {_pod_name()}") from exc
        return count > 0


def _config_payload(config_dir: str) -> dict:
    """Flatten the staged ``/config`` tree into ConfigMap keys.

    Nested paths (``files/node.py``) become ``files__node.py`` and are restored by the
    pod's init step, because a ConfigMap key cannot contain a slash.
    """
    data, total = {}, 0
    for root, _dirs, names in os.walk(config_dir):
        for name in names:
            path = os.path.join(root, name)
            rel = os.path.relpath(path, config_dir)
            with open(path, "rb") as f:
                raw = f.read()
            total += len(raw)
            if total > _CONFIGMAP_LIMIT_BYTES:
                raise ValueError(
                    "the staged config exceeds what a ConfigMap can carry "
                    f"(~{_CONFIGMAP_LIMIT_BYTES // 1024} KiB); run this config as a "
                    "campaign instead")
            data[rel.replace(os.sep, "__")] = raw.decode("utf-8", "replace")
    return data


def _configmap_manifest(data: dict, namespace: str, owner_ref: dict | None) -> dict:
    metadata = {"name": _pod_name(), "namespace": namespace,
                "labels": dict(_labels())}
    if owner_ref:
        metadata["ownerReferences"] = [owner_ref]
    return {"apiVersion": "v1", "kind": "ConfigMap", "metadata": metadata, "data": data}


def _pod_manifest(spec: ExecSpec, deadline_s: int, namespace: str,
                  owner_ref: dict | None) -> dict:
    """A single kept-alive container with ``/config`` restored from the ConfigMap.

    ``activeDeadlineSeconds`` is the manager's own deadline, so the pod cannot outlive
    the service's intent even if the reaper never runs. An init container rebuilds the
    directory layout the flattened ConfigMap keys encode.
    """
    metadata = {"name": _pod_name(), "namespace": namespace, "labels": dict(_labels())}
    if owner_ref:
        metadata["ownerReferences"] = [owner_ref]
    env = [{"name": k, "value": str(v)} for k, v in spec.env.items()]
    restore = (
        'set -e; for f in /raw/*; do n=$(basename "$f"); '
        'case "$n" in .*) continue;; esac; '
        'out="/config/$(echo "$n" | sed "s|__|/|g")"; mkdir -p "$(dirname "$out")"; '
        'cp "$f" "$out"; done; chmod +x /config/entrypoint.sh 2>/dev/null || true')
    return {
        "apiVersion": "v1", "kind": "Pod", "metadata": metadata,
        "spec": {
            "restartPolicy": "Never",
            "activeDeadlineSeconds": int(deadline_s),
            "initContainers": [{
                "name": "restore-config", "image": spec.image,
                "imagePullPolicy": "IfNotPresent",
                "command": ["/bin/sh", "-c", restore],
                "volumeMounts": [{"name": "raw", "mountPath": "/raw"},
                                 {"name": "config", "mountPath": "/config"}],
            }],
            "containers": [{
                "name": _CONTAINER, "image": spec.image,
                "imagePullPolicy": "IfNotPresent",
                # Idle PID 1, so exec'd commands run against a stable container and
                # anything backgrounded has something to reparent to.
                "command": ["/bin/bash", "-c", f"exec sleep {int(deadline_s)}"],
                "env": env,
                "volumeMounts": [{"name": "config", "mountPath": "/config"}],
            }],
            "volumes": [
                {"name": "raw", "configMap": {"name": _pod_name()}},
                {"name": "config", "emptyDir": {}},
            ],
        },
    }


def _labels() -> dict:
    key, _, value = POD_LABEL.partition("=")
    return {key: value}


def _wait_ready(core, namespace: str, name: str, timeout_s: float = 120.0) -> None:
    """Block until the pod can be exec'd into, or fail saying why it cannot."""
    import time
    deadline = time.monotonic() + timeout_s
    last = ""
    while time.monotonic() < deadline:
        pod = core.read_namespaced_pod(name, namespace)
        phase = pod.status.phase
        if phase == "Running":
            return
        if phase in ("Failed", "Succeeded"):
            raise RuntimeError(f"exec pod ended before it could be used ({phase})")
        last = _pending_reason(pod) or phase or ""
        time.sleep(2)
    raise RuntimeError(f"exec pod not ready after {int(timeout_s)}s: {last}")


def _pending_reason(pod) -> str:
    """The most useful line from a pending pod — an image pull error, say."""
    for statuses in (pod.status.init_container_statuses,
                     pod.status.container_statuses):
        for status in statuses or []:
            waiting = getattr(status.state, "waiting", None)
            if waiting and waiting.reason:
                return f"{waiting.reason}: {waiting.message or ''}".strip()
    return ""
