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

_connect_timeout_installed = False


def _install_default_connect_timeout() -> None:
    """Give every Kubernetes API call a connect timeout, once per process.

    The generated client takes a timeout only per call (``_request_timeout``) and,
    when it is absent, passes ``timeout=None`` down to urllib3 — which *overrides* any
    default configured on the connection pool. So its own request method is the only
    place a default can live. An explicit ``_request_timeout`` still wins, and the
    websocket path (``kubernetes.stream``, which replaces ``ApiClient.request`` a layer
    above this one) is untouched.
    """
    global _connect_timeout_installed  # noqa: PLW0603 - process-wide client policy
    if _connect_timeout_installed:
        return
    from kubernetes.client import rest

    original = rest.RESTClientObject.request

    @functools.wraps(original)
    def request(self, *args, _request_timeout=None, **kwargs):
        if _request_timeout is None:
            _request_timeout = (CONNECT_TIMEOUT_SECONDS, None)
        return original(self, *args, _request_timeout=_request_timeout, **kwargs)

    rest.RESTClientObject.request = request
    _connect_timeout_installed = True


def load_kube_config(context: str | None = None) -> str:
    """Load kube client config: in-cluster service account first, else host kubeconfig.

    Also installs the process-wide connect timeout
    (:func:`_install_default_connect_timeout`) — this is the one entry point every
    cluster-touching path already goes through, so the policy cannot be missed.

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
