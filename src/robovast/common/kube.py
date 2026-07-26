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

It **fails loudly** when neither source is available, instead of letting a raw or
partial ``kubernetes`` error leak differently out of each call site. It does not
warn on the in-cluster→host step itself: off-cluster (a laptop, CI) that step is
the *normal* path and the host's current context is exactly what the operator
expects — a per-call warning there would be pure noise.

Lives in ``common`` because it is foundational infrastructure whose only
dependency is the third-party ``kubernetes`` package; every cluster/service
module depends on it *downward*.
"""

import logging

logger = logging.getLogger(__name__)


def load_kube_config(context: str | None = None) -> str:
    """Load kube client config: in-cluster service account first, else host kubeconfig.

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
