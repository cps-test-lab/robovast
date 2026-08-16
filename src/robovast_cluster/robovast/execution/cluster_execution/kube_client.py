# Copyright (C) 2026 Frederik Pasch
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions
# and limitations under the License.
#
# SPDX-License-Identifier: Apache-2.0

"""The one way to load Kubernetes client configuration.

Every cluster-touching path needs the same decision — use the in-pod service
account when running inside the cluster, otherwise the host kubeconfig — and it
had been copy-pasted ~10 times with subtly different exception handling: some
caught ``ConfigException``, some a bare ``Exception``; some threaded a context,
some dropped it. This is the single implementation.

It also owns the two client-wide policies that only make sense in one place: every API
call gets a connect timeout (so an unreachable cluster fails in seconds, not minutes),
and :func:`api_transport_errors` turns a failed transport into one sentence naming the
host instead of a urllib3 retry traceback.

Finally it holds the **kept-alive pod** primitives — :func:`wait_pod_ready`,
:func:`wait_pod_gone` and :func:`exec_stream`. Two subsystems run a pod and exec into it
(the per-campaign aux pod for variation plugins, and the diagnostic container-exec lane),
they had a copy each, and the copies were not equally correct: only one reported *why* a
pod was not starting, only one waited for a delete to finish, and only one bounded an
exec. Sharing them is what makes those three properties true in both places at once.
They live here rather than beside either caller because
``tests/execution/test_layering.py`` forbids the execution engine from importing
``robovast.service``, and ``common`` is the only place both sides may depend on.

It **fails loudly** when neither source is available, instead of letting a raw or
partial ``kubernetes`` error leak differently out of each call site. It does not
warn on the in-cluster→host step itself: off-cluster (a laptop, CI) that step is
the *normal* path and the host's current context is exactly what the operator
expects — a per-call warning there would be pure noise.

Lives in ``common`` because it is foundational infrastructure whose only
dependency is the third-party ``kubernetes`` package; every cluster/service
module depends on it *downward*.
"""

import contextlib
import functools
import logging
import os

logger = logging.getLogger(__name__)

#: Seconds to wait for the TCP connection to the API server, overridable with
#: ``ROBOVAST_KUBE_CONNECT_TIMEOUT``. Without it a connect that can never succeed
#: (cluster stopped, VPN down) blocks for the OS TCP timeout — minutes, once per
#: urllib3 retry — before the failure is even reported. Read timeouts stay unlimited:
#: a slow answer is still an answer, and pod-log reads are legitimately long.
CONNECT_TIMEOUT_SECONDS = float(os.environ.get("ROBOVAST_KUBE_CONNECT_TIMEOUT", "10"))

_CONNECT_TIMEOUT_INSTALLED = False


def _install_default_connect_timeout() -> None:
    """Give every Kubernetes API call a connect timeout, once per process.

    The generated client takes a timeout only per call (``_request_timeout``) and,
    when it is absent, passes ``timeout=None`` down to urllib3 — which *overrides* any
    default configured on the connection pool. So its own request method is the only
    place a default can live. An explicit ``_request_timeout`` still wins, and the
    websocket path (``kubernetes.stream``, which replaces ``ApiClient.request`` a layer
    above this one) is untouched.
    """
    global _CONNECT_TIMEOUT_INSTALLED  # pylint: disable=global-statement
    # Process-wide client policy: the patch is on the generated client's own
    # request method, so 'once per process' is the only correct scope.
    if _CONNECT_TIMEOUT_INSTALLED:
        return
    from kubernetes.client import rest

    original = rest.RESTClientObject.request

    @functools.wraps(original)
    def request(self, *args, _request_timeout=None, **kwargs):
        if _request_timeout is None:
            _request_timeout = (CONNECT_TIMEOUT_SECONDS, None)
        return original(self, *args, _request_timeout=_request_timeout, **kwargs)

    rest.RESTClientObject.request = request
    _CONNECT_TIMEOUT_INSTALLED = True


def load_kube_config(context: str | None = None) -> str:
    """Load kube client config: in-cluster service account first, else host kubeconfig.

    Also installs the process-wide connect timeout
    (:func:`_install_default_connect_timeout`), which is the reason every
    cluster-touching path must come through here rather than calling
    ``kubernetes.config`` itself.

    That used to be asserted rather than checked, and it was false in ten places: the
    cluster-config providers, the service deploy/cleanup paths and the RBAC setup all
    loaded config directly, so their API calls ran with ``timeout=None``. The visible
    cost was an off-cluster ``vast serve --backend cluster`` hanging for minutes on an
    unreachable cluster and then dying in a urllib3 traceback. A test now enforces it
    (``tests/common/test_kube_loader_is_the_only_entry.py``).

    Args:
        context: Host kubeconfig context to select when not running in-cluster.
            ``None`` uses the kubeconfig's current context. Ignored in-cluster
            (the pod's service account is unambiguous).

    Returns:
        A short description of what was loaded — ``"in-cluster"`` or
        ``"host:<context>"`` — for logging/diagnostics.

    Raises:
        RuntimeError: neither an in-cluster service account nor a usable host
            kubeconfig is available. The message names both failures so the cause
            (wrong pod RBAC vs. missing/!broken kubeconfig) is unambiguous.
    """
    from kubernetes import config

    _install_default_connect_timeout()
    try:
        config.load_incluster_config()
        logger.debug("Loaded in-cluster Kubernetes config")
        return "in-cluster"
    except config.ConfigException as incluster_exc:
        try:
            config.load_kube_config(context=context)
        except Exception as host_exc:
            raise RuntimeError(
                "no Kubernetes configuration available: not running inside a "
                f"cluster ({incluster_exc}); and no usable host kubeconfig"
                f"{f' for context {context!r}' if context else ''} ({host_exc})"
            ) from host_exc
        loaded = f"host:{context or 'current-context'}"
        logger.debug("Loaded host Kubernetes config (%s)", loaded)
        return loaded


def pod_pending_reason(pod) -> str:
    """The most useful line from a pod that is not Running yet, or ``""``.

    A pending pod's phase is just ``"Pending"``; the reason it is stuck — ``ImagePullBackOff``,
    ``CreateContainerConfigError`` — lives on a container status. Reporting the phase alone
    turns a bad image reference into a timeout with no cause, which is how it read for the
    aux pod: five minutes of silence, then "was not Running within 300s".

    Init containers are checked first: they run before the main one, so when both are
    waiting the init container's reason is the one that explains the other.
    """
    for statuses in (pod.status.init_container_statuses, pod.status.container_statuses):
        for status in statuses or []:
            waiting = getattr(status.state, "waiting", None)
            if waiting and waiting.reason:
                return f"{waiting.reason}: {waiting.message or ''}".strip()
    return ""


def pod_workload_containers(pod) -> list:
    """*pod*'s containers that run for its whole life, main first then sidecars.

    Kubernetes overloads "init container": one with ``restartPolicy: Always`` is a
    **native sidecar**, which kubelet starts before the regular containers and stops only
    after the last one exits. It is a workload container that happens to be declared in
    ``initContainers`` — the opposite of what the field name suggests. Ordinary init
    containers (``s3-init``, which populates ``/config`` and exits) are one-shot staging
    and excluded.

    Anything asking "which containers does this pod actually run?" must ask it here.
    Three places answered it from ``spec.containers`` alone and each was wrong in the same
    way once the simulator and the system under test became sidecars: resource accounting
    dropped the two biggest reservations, image pinning pinned every role to the scenario's
    digest, and the job log showed one container out of three.

    Returns the container *specs*, not names — callers need ``.resources`` as often as
    ``.name``.
    """
    spec = getattr(pod, "spec", None)
    if spec is None:
        return []
    regular = list(getattr(spec, "containers", None) or [])
    sidecars = [c for c in (getattr(spec, "init_containers", None) or [])
                if getattr(c, "restart_policy", None) == "Always"]
    return regular + sidecars


def wait_pod_ready(core, namespace: str, name: str, timeout_s: float = 120.0) -> None:
    """Block until *name* can be exec'd into, or fail saying why it cannot.

    Raises:
        RuntimeError: the pod reached a terminal phase before it could be used, or it was
            still not Running at *timeout_s* — in which case the message carries
            :func:`pod_pending_reason` rather than only the elapsed time.
    """
    import time

    deadline = time.monotonic() + timeout_s
    last = ""
    while time.monotonic() < deadline:
        pod = core.read_namespaced_pod(name, namespace)
        phase = pod.status.phase
        if phase == "Running":
            return
        if phase in ("Failed", "Succeeded"):
            raise RuntimeError(f"pod {name} ended before it could be used ({phase})")
        last = pod_pending_reason(pod) or phase or ""
        time.sleep(2)
    raise RuntimeError(f"pod {name} not ready after {int(timeout_s)}s: {last}")


def wait_pod_gone(core, namespace: str, name: str, reads=None,
                  timeout_s: float = 120.0) -> None:
    """Block until *name* is really gone, for each API kind in *reads*.

    The wait is the point. A Kubernetes delete returns while the object is still
    ``Terminating``, so a caller that immediately re-creates gets ``AlreadyExists`` — or,
    worse, treats the 409 as "it already exists, reuse it" and adopts a corpse that will
    never become Running. ``docker rm -f`` is synchronous and both callers' contracts are
    written against that behaviour.

    Args:
        reads: read functions taking ``(name, namespace)``. Defaults to just
            ``core.read_namespaced_pod``; pass more when a delete spans several kinds.

    Raises:
        RuntimeError: something was still terminating at *timeout_s*.
    """
    import time

    from kubernetes.client.rest import ApiException

    deadline = time.monotonic() + timeout_s
    for read in (reads if reads is not None else (core.read_namespaced_pod,)):
        while time.monotonic() < deadline:
            try:
                read(name, namespace)
            except ApiException as e:
                if e.status == 404:
                    break
                raise
            time.sleep(1)
        else:
            raise RuntimeError(
                f"{name} did not finish terminating within {int(timeout_s)}s")


def exec_stream(core, pod: str, namespace: str, container: str, command,
                *, limit_s: float, stdin_data: str | None = None,
                on_stdout_line=None, on_stderr_line=None):
    """Exec *command* in a running pod. Returns ``(code, stdout, stderr, timed_out)``.

    The in-cluster equivalent of ``docker exec``, and the one implementation of it. A
    timed-out exec reports ``124`` with whatever was collected, mirroring the local lane's
    ``subprocess`` timeout, rather than returning ``None`` for the exit code.

    *limit_s* is a real bound, not a poll interval. Without one this loop spins for as long
    as the command runs, which is fine until the command never finishes — a plugin's helper
    hanging then hangs the campaign worker with it, silently and forever.

    Note on *stdin_data*: the stream can be written to but **cannot be half-closed**, so a
    receiver waiting for EOF never sees one. A sender must frame its payload by length (see
    ``ClusterContainerRunner._copy_in``); this function cannot do it for the caller.
    """
    import time

    from kubernetes.stream import stream

    resp = stream(core.connect_get_namespaced_pod_exec, pod, namespace,
                  container=container, command=list(command),
                  stderr=True, stdin=stdin_data is not None, stdout=True,
                  tty=False, _preload_content=False)
    out, err = [], []
    deadline = time.monotonic() + max(1.0, float(limit_s))
    timed_out = False
    try:
        if stdin_data is not None:
            resp.write_stdin(stdin_data)
        while resp.is_open():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            # Poll in short slices so the deadline is honoured to the second, rather than
            # blocking for the whole remaining budget in one update() call.
            resp.update(timeout=min(1.0, remaining))
            if resp.peek_stdout():
                chunk = resp.read_stdout()
                out.append(chunk)
                if on_stdout_line:
                    for line in chunk.splitlines():
                        on_stdout_line(line)
            if resp.peek_stderr():
                chunk = resp.read_stderr()
                err.append(chunk)
                if on_stderr_line:
                    for line in chunk.splitlines():
                        on_stderr_line(line)
        # `resp.returncode` int()s the exec status the API server returns. When the exec
        # never STARTS -- a missing executable, a bad working dir -- that status carries a
        # message instead of an exit code, so the client raises
        # `ValueError: invalid literal for int()` whose text is the real error, mangled.
        # A caller then reports an int-parsing bug in RoboVAST for what is actually
        # "executable file not found in $PATH". Keep the message, report a failure.
        if timed_out:
            code = None
        else:
            try:
                code = resp.returncode
            except (ValueError, TypeError) as exc:
                err.append(f"exec did not start: {exc}")
                code = 126  # the shell's "command found but not executable" convention
    finally:
        resp.close()
    if code is None:
        # Either the deadline fired, or the channel closed without a status — neither is a
        # success, and reporting 0 for the second would invent one.
        timed_out, code = True, 124
    return code, "".join(out), "".join(err), timed_out


@contextlib.contextmanager
def quiet_urllib3_retries():
    """Silence urllib3's per-attempt "Retrying (Retry(...))" warnings.

    Those are printed by the transport while it is still deciding whether the call
    fails; the caller decides what the failure *means* and says so once. Both the
    parent and the ``connectionpool`` child logger must be raised, because the child
    may carry its own effective level.
    """
    loggers = [logging.getLogger("urllib3"), logging.getLogger("urllib3.connectionpool")]
    previous = [lg.level for lg in loggers]
    for lg in loggers:
        lg.setLevel(logging.CRITICAL)
    try:
        yield
    finally:
        for lg, level in zip(loggers, previous):
            lg.setLevel(level)


@contextlib.contextmanager
def api_transport_errors(what: str):
    """Translate a failed Kubernetes API *transport* into a clean error.

    Anything urllib3 raises (connect timeout, DNS failure, refused connection, a
    broken connection mid-response) means the API server never answered — a fact one
    sentence states completely. Without this the raw ``MaxRetryError`` reaches the
    caller's generic handler and prints a ~60-line traceback through urllib3's retry
    internals, which tells an operator with a stopped cluster nothing at all.

    Args:
        what: What was being attempted, phrased to follow "while" — e.g.
            ``"checking the Kueue admission path"``.

    Raises:
        ClusterUnreachableError: naming the API host, the attempted operation, and
            the transport error. Every other exception passes through untouched.
    """
    import urllib3.exceptions
    from kubernetes import client

    from robovast.common.errors import ClusterUnreachableError

    with quiet_urllib3_retries():
        try:
            yield
        except urllib3.exceptions.HTTPError as exc:
            host = client.Configuration.get_default_copy().host or "the configured host"
            # MaxRetryError stringifies to pool + URL + cause; the pool and URL only
            # repeat the host and the endpoint already named here, so quote the cause.
            cause = getattr(exc, "reason", None) or exc
            raise ClusterUnreachableError(
                f"Kubernetes API server {host} is unreachable while {what}: {cause}. "
                "Check that the cluster is running and reachable (VPN, kubeconfig "
                "context, 'kubectl cluster-info')."
            ) from exc
