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

"""MCP plugin exposing robovast's plugin extension system.

Provides tools for discovering and describing all plugin extension groups
registered in ``importlib.metadata``, including MCP plugins, CLI plugins,
cluster backends, variation strategies, post-processing steps, and more.
"""

import fnmatch
import logging
import textwrap
from importlib.metadata import entry_points

from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Static catalogue of all known robovast extension groups
# ---------------------------------------------------------------------------

_PLUGIN_GROUPS: dict[str, dict] = {
    "robovast.mcp_plugins": {
        "description": (
            "MCP server tools and resources. Plugins can provide new tools."
        ),
        "loader_module": "robovast.evaluation.mcp_server.registry",
    },
    "robovast.cli_plugins": {
        "description": (
            "CLI sub-commands exposed under the ``vast`` entry point "
            "(e.g. ``vast execution``, ``vast results``)."
        ),
        "loader_module": "robovast.common.cli.cli",
    },
    "robovast.cluster_configs": {
        "description": (
            "Kubernetes cluster backend configuration used during distributed execution "
            "(minikube, RKE2, GCP, Azure …)."
        ),
        "loader_module": "robovast.execution.cluster_execution.cluster_setup",
    },
    "robovast.share_providers": {
        "description": (
            "File-share backends for uploading campaign output (e.g. Nextcloud)."
        ),
        "loader_module": "robovast.execution.cluster_execution.share_providers",
    },
    "robovast.variation_types": {
        "description": (
            "Parameter variation strategies applied during scenario generation "
            "(list, uniform distribution, Gaussian distribution …)."
        ),
        "loader_module": "robovast.common.variation.loader",
    },
    "robovast.postprocessing_commands": {
        "description": (
            "Post-processing pipeline steps executed after each run "
            "(rosbag to CSV/WebM conversion, custom shell commands …)."
        ),
        "loader_module": "robovast.results_processing.postprocessing",
    },
    "robovast.publication_plugins": {
        "description": (
            "Export/publication backends for shipping results "
            "(e.g. ZIP archive)."
        ),
        "loader_module": "robovast.results_processing.publication",
    },
    "robovast.metadata_processing": {
        "description": (
            "Metadata enrichment processors run after execution to augment "
            "campaign metadata with derived information."
        ),
        "loader_module": "robovast.results_processing.metadata",
    },
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load_doc(ep, max_lines: int = 1) -> str | None:  # type: ignore[type-arg]
    """Load *ep* and return up to *max_lines* lines of its docstring, or ``None``.

    With the default of ``max_lines=1`` only the summary line is returned.
    Pass a larger value (or ``0`` for unlimited) to get more detail.
    """
    try:
        obj = ep.load()
        raw = getattr(obj, "__doc__", None) or ""
        lines = [l for l in textwrap.dedent(raw).strip().splitlines() if l.strip()]
        if not lines:
            return None
        selected = lines if max_lines == 0 else lines[:max_lines]
        return " ".join(selected) or None
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not load %r for doc extraction: %s", ep.value, exc)
        return None


# ---------------------------------------------------------------------------
# Tool functions
# ---------------------------------------------------------------------------

def list_plugin_groups() -> list[dict]:
    """List all RoboVAST plugin extension groups.

    Returns the complete catalogue of known extension groups with their
    group key, a human-readable description, and the module responsible
    for loading them at runtime.
    """
    return [
        {
            "group": group,
            "description": meta["description"],
            "loader_module": meta["loader_module"],
        }
        for group, meta in _PLUGIN_GROUPS.items()
    ]


def list_plugins(group: str = "") -> list[dict]:
    """List all installed plugins for a given extension group.

    If group is not set, all plugins across all groups are returned.

    Enumerates ``importlib.metadata`` entry points for *group* and returns
    one record per plugin with its entry-point name, dotted import target,
    and the first paragraph of the class or function docstring.

    Works for any group name – not limited to the eight built-in groups.

    Args:
        group: Entry-point group name, e.g. ``"robovast.mcp_plugins"``
               or ``"robovast.postprocessing_commands"``.
    """
    groups = []
    if group:
        groups = [group] if group in _PLUGIN_GROUPS else []
    else:
        groups = list(_PLUGIN_GROUPS.keys())
    
    for grp in groups:
        records = []
        for ep in entry_points(group=grp):
            records.append(
                {
                    "name": ep.name,
                    "class": ep.value,
                    "doc": _load_doc(ep),
                }
            )
        return sorted(records, key=lambda r: r["name"])
    return [{"error": f"No plugins found in group '{group}'."}]


def search_plugin(name: str) -> list[dict]:
    """Search for a plugin by name across all extension groups.

    Matches the entry-point name case-insensitively across every known
    robovast extension group.  Two matching modes are supported:

    * **Wildcard** – when *name* contains ``*`` or ``?``, it is treated as a
      ``fnmatch``-style glob pattern (e.g. ``"Floor*"``, ``"*Generation*"``).
    * **Substring** – otherwise a plain case-insensitive substring match is
      used (e.g. ``"FloorplanGeneration"``).

    Each result includes:

    * ``group`` – the extension group the plugin is registered in.
    * ``name`` – exact entry-point name.
    * ``class`` – dotted import target (``"module:ClassName"``).
    * ``doc`` – first paragraph of the plugin's docstring, if available.

    Args:
        name: Substring or wildcard pattern to match against plugin names
              (case-insensitive).
    """
    use_glob = any(c in name for c in ("*", "?", "["))
    needle = name.lower()
    results = []
    for group in _PLUGIN_GROUPS:
        for ep in entry_points(group=group):
            ep_name_lower = ep.name.lower()
            matched = (
                fnmatch.fnmatch(ep_name_lower, needle)
                if use_glob
                else needle in ep_name_lower
            )
            if matched:
                results.append(
                    {
                        "group": group,
                        "name": ep.name,
                        "class": ep.value,
                        "doc": _load_doc(ep),
                    }
                )
    return sorted(results, key=lambda r: (r["group"], r["name"]))


def get_plugin_details(group: str, name: str, max_lines: int = 0) -> dict:
    """Get full details for a specific plugin.

    Loads the entry point identified by *group* and *name* and returns its
    import target together with the plugin's docstring.

    Args:
        group: Extension group name, e.g. ``"robovast.postprocessing_commands"``.
        name: Exact entry-point name, e.g. ``"FloorplanGeneration"``.
        max_lines: Maximum number of non-blank docstring lines to return.
                   ``0`` (default) means unlimited – the full docstring.
    """
    matches = [ep for ep in entry_points(group=group) if ep.name == name]
    if not matches:
        return {"error": f"No plugin '{name}' found in group '{group}'."}
    ep = matches[0]
    return {
        "group": group,
        "name": ep.name,
        "class": ep.value,
        "doc": _load_doc(ep, max_lines=max_lines),
    }


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------

_TOOLS = [
    list_plugin_groups,
    list_plugins,
    search_plugin,
    get_plugin_details,
]


class PluginMetadataPlugin:
    name = "plugin_metadata"

    def register(self, mcp: FastMCP) -> None:
        for fn in _TOOLS:
            mcp.tool()(fn)
