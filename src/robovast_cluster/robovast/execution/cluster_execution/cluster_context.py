# Copyright (C) 2025 Frederik Pasch
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

"""Kubernetes context awareness and per-cluster resource resolution.

Resource values in the ``.vast`` config file may be given as a per-cluster
list keyed by the real Kubernetes context name instead of a scalar:

.. code-block:: yaml

    resources:
      cpu:
        - gke_my-project_us-central1_my-cluster: 4
        - minikube: 8
      memory:
        - gke_my-project_us-central1_my-cluster: 10Gi
        - minikube: 20Gi

Scalars always work and are the recommended default when a single cluster is
used.  The service resolves a per-cluster list with the context it was deployed
against, which ``vast cluster setup`` and ``vast service upgrade`` record: the one
named with ``--context/-x``, else the kubeconfig's current context.
"""

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Active Kubernetes context
# ---------------------------------------------------------------------------

def get_active_kube_context() -> Optional[str]:
    """Return the name of the currently active Kubernetes context.

    Reads the active context from the local kubeconfig (``~/.kube/config``
    or ``KUBECONFIG``).  Returns ``None`` when the context cannot be
    determined (e.g. kubeconfig is absent).
    """
    try:
        from kubernetes import config as kube_config  # pylint: disable=import-outside-toplevel
        _, active = kube_config.list_kube_config_contexts()
        return active["name"] if active else None
    except Exception as exc:
        logger.debug(f"Could not determine active kube context: {exc}")
        return None


def list_all_contexts() -> list[tuple[str, str]]:
    """List all available ``(label, kube_context_name)`` pairs from the kubeconfig.

    Returns:
        List of ``(label, kube_context_name)`` tuples sorted by name.
        Returns an empty list when no kubeconfig is available.
    """
    try:
        from kubernetes import config as kube_config  # pylint: disable=import-outside-toplevel
        contexts, _ = kube_config.list_kube_config_contexts()
        return sorted((c["name"], c["name"]) for c in (contexts or []))
    except Exception as exc:
        logger.debug(f"Could not list kube contexts: {exc}")
        return []


# ---------------------------------------------------------------------------
# Resource value resolution
# ---------------------------------------------------------------------------

def resolve_resource_value(
    value: Any,
    context: Optional[str],
) -> Any:
    """Resolve a resource value for the active Kubernetes context.

    Handles two forms:

    * **Scalar** (``int``, ``float``, or ``str``): returned as-is.
    * **Per-cluster list** (``[{context-name: value}, …]``): the entry whose
      key matches *context* is returned.

    Raises:
        ValueError: When the value is a per-cluster list but *context* is
                    ``None`` -- the service has no context recorded -- or when the
                    context has no entry in the list.

    Args:
        value: Raw resource value (scalar or per-cluster list).
        context: The context the service was deployed against, or ``None``.

    Returns:
        Resolved scalar value, or ``None`` when *value* is ``None``.
    """
    if value is None:
        return None
    if isinstance(value, (int, float, str)):
        return value
    if isinstance(value, list):
        if not value:
            return None
        if context is None:
            available = [list(e.keys())[0] for e in value if isinstance(e, dict) and e]
            raise ValueError(
                f"Per-cluster resource list {available} found but this service has no "
                "Kubernetes context recorded to pick an entry with. The service records "
                "the context it is deployed against -- the one named with --context/-x, "
                "else the kubeconfig's current one: run 'vast service upgrade' against "
                "this cluster to record it, or replace the per-cluster list with a plain "
                "scalar value."
            )
        for entry in value:
            if isinstance(entry, dict) and context in entry:
                return entry[context]
        available = [list(e.keys())[0] for e in value if isinstance(e, dict) and e]
        raise ValueError(
            f"No resource entry found for context '{context}'. "
            f"Available contexts in the per-cluster list: {available}. "
            f"Add a '{context}' entry or use a plain scalar value."
        )
    return value


def resolve_resources(
    resources: dict,
    context: Optional[str],
) -> dict:
    """Resolve all resource fields in a resources dict for the active cluster.

    Calls :func:`resolve_resource_value` for every key in *resources* and
    returns a new dict with all per-cluster lists replaced by their resolved
    scalar values.

    Raises:
        ValueError: Propagated from :func:`resolve_resource_value` when a
                    per-cluster list has no entry for *context*.

    Args:
        resources: Raw resources dict (e.g. ``{'cpu': 15}`` or
                   ``{'cpu': [{'gke_my-project_…_cluster': 4}, {'minikube': 8}]}``).
        context: Active Kubernetes context name, or ``None``.

    Returns:
        New dict with resolved scalar values (``None`` entries removed).
    """
    resolved = {}
    for key, val in resources.items():
        r = resolve_resource_value(val, context)
        if r is not None:
            resolved[key] = r
    return resolved
