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
used.  Pass the matching context name via ``--context/-x`` when running
commands against a specific cluster.
"""

import logging
from typing import Any, Optional

import yaml

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
# Config-file context name scanning
# ---------------------------------------------------------------------------

def get_config_context_names(config_path: str) -> set[str]:
    """Extract all context names used in per-cluster resource lists.

    Scans a ``.vast`` YAML config for any field that uses the per-cluster list
    syntax (``[{context-name: value}, …]``) and returns the union of all keys.

    Args:
        config_path: Absolute path to a ``.vast`` YAML config file.

    Returns:
        Set of context name strings.  Empty when no per-cluster lists are found.
    """
    try:
        with open(config_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except Exception as exc:
        logger.debug(f"Could not read config file {config_path!r}: {exc}")
        return set()

    names: set[str] = set()

    # Only the resource fields that support per-cluster lists
    _resource_fields = frozenset({"cpu", "memory"})

    def _scan(node: Any) -> None:
        if isinstance(node, dict):
            for key, v in node.items():
                if key in _resource_fields and isinstance(v, list):
                    # Per-cluster resource list: every item must be a single-key dict
                    if v and all(isinstance(item, dict) and len(item) == 1 for item in v):
                        for item in v:
                            names.update(item.keys())
                else:
                    _scan(v)
        elif isinstance(node, list):
            for item in node:
                _scan(item)

    _scan(data)
    return names


def require_context_for_multi_cluster(kube_context: Optional[str]) -> None:
    """Raise :exc:`ValueError` when a multi-cluster config is used without ``--context``.

    Discovers the project ``.vast`` config file, scans it for per-cluster
    resource lists, and raises an informative error when more than one context
    name is present and no *kube_context* was specified.

    This is a no-op when:

    * *kube_context* is already set (the user supplied ``--context``).
    * No project config can be found.
    * The config uses only a single context name (or only plain scalars).

    Args:
        kube_context: The Kubernetes context name (``None`` when the user did
                      not pass ``--context``).

    Raises:
        ValueError: When multiple context names are found and *kube_context*
                    is ``None``.
    """
    if kube_context is not None:
        return

    try:
        from robovast.common.cli.project_config import ProjectConfig  # pylint: disable=import-outside-toplevel  # local import – avoid cycles
        pc = ProjectConfig.load()
        config_path = pc.config_path if pc else None
    except Exception:
        config_path = None

    if not config_path:
        return

    names = get_config_context_names(config_path)
    if len(names) <= 1:
        return

    raise ValueError(
        f"The .vast config uses per-cluster resource lists for multiple contexts {sorted(names)}. "
        f"Please specify --context/-x to select the target cluster."
    )


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
                    ``None``, or when the context has no entry in the list.

    Args:
        value: Raw resource value (scalar or per-cluster list).
        context: Active Kubernetes context name, or ``None``.

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
                f"Per-cluster resource list {available} found but no Kubernetes context was "
                "specified. Use --context/-x to select a target cluster, "
                "or replace the per-cluster list with a plain scalar value."
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
