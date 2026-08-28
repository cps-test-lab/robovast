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

Discovers and describes the extension groups registered in ``importlib.metadata`` — MCP
plugins, CLI plugins, cluster backends, variation strategies, postprocessing steps.

``list_plugins`` answers three questions that were three tools (the group catalog, one
group's plugins, and a name search). They differ only in which filter is applied, so the
distinction cost the caller three tool schemas to read and a choice to get wrong, while
the implementation was one enumeration behind three signatures.
"""

import fnmatch
import logging
import textwrap
from importlib.metadata import entry_points

from fastmcp import FastMCP

from robovast.common.plugin_schema import schema_from_object

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Static catalog of all known robovast extension groups
# ---------------------------------------------------------------------------

_PLUGIN_GROUPS: dict[str, dict] = {
    "robovast.mcp_plugins": {
        "description": (
            "MCP server tools and resources. Plugins can provide new tools."
        ),
        "loader_module": "robovast.mcp_server.registry",
    },
    "robovast.cli_plugins": {
        "description": (
            "CLI sub-commands exposed under the ``vast`` entry point "
            "(e.g. ``vast cluster``, ``vast results``)."
        ),
        "loader_module": "robovast.client.cli",
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
        "loader_module": "robovast.execution.share_providers",
    },
    "robovast.search_strategies": {
        "description": (
            "Search strategies that propose the next configurations to run "
            "(random, halton, boundary, qd, optuna …), named by ``search.strategy``."
        ),
        "loader_module": "robovast.search.strategy",
    },
    "robovast.extractors": {
        "description": (
            "Extractors that reduce a run's output to the objective a search optimises, "
            "named by ``search.extract.plugin``."
        ),
        "loader_module": "robovast.search.evaluator",
    },
    "robovast.simulators": {
        "description": (
            "Simulator backends the execution lane can drive, named by "
            "``execution.containers.simulation.backend``."
        ),
        "loader_module": "robovast.common.simulators",
    },
    "robovast.variation_types": {
        "description": (
            "Parameter variation strategies applied during scenario generation "
            "(list, uniform distribution, Gaussian distribution …)."
        ),
        "loader_module": "robovast.common.variation.loader",
    },
    "robovast.input_generators": {
        "description": (
            "Derived campaign inputs produced before composition, declared as "
            "``execution.generate`` (a map compiled from a floorplan, a browser scene "
            "descriptor compiled from a simulation world …). Their outputs join "
            "``run_files``, so they are hashed into the config identity."
        ),
        "loader_module": "robovast.common.input_generation",
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
    "robovast.panel_types": {
        "description": (
            "Web panels a ``.vast`` may declare. One group for both surfaces, told apart by each "
            "class's ``SURFACE``: ``run`` panels go under "
            "``visualization.results.run_view.panels`` and ``config`` panels under "
            "``visualization.config.panels`` -- see the ``surface`` field of each row."
        ),
        "loader_module": "robovast.common.config",
    },
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

#: Panel types are one group covering two surfaces, so a row that does not say which is ambiguous
#: exactly where it matters: naming a run panel in the config column is refused, and vice versa.
_PANEL_TYPES_GROUP = "robovast.panel_types"


def _panel_surface(ep) -> str | None:
    """The ``SURFACE`` a panel plugin declares, or the default when it declares none.

    Best-effort like every other reflection here: a plugin that fails to import gets ``None``
    rather than a guess, because "run" would be a wrong answer rather than a missing one.
    """
    try:
        from robovast.common.config import \
            DEFAULT_PANEL_SURFACE  # pylint: disable=import-outside-toplevel
        return getattr(ep.load(), "SURFACE", DEFAULT_PANEL_SURFACE)
    except Exception:  # noqa: BLE001 - a broken plugin must not break the listing
        logger.debug("could not read SURFACE from panel plugin %r", ep.name)
        return None


def _doc_from_obj(obj, max_lines: int = 1) -> str | None:
    """Return up to *max_lines* lines of *obj*'s docstring, or ``None``.

    With the default of ``max_lines=1`` only the summary line is returned.
    Pass a larger value (or ``0`` for unlimited) to get more detail.
    """
    raw = getattr(obj, "__doc__", None) or ""
    lines = [l for l in textwrap.dedent(raw).strip().splitlines() if l.strip()]
    if not lines:
        return None
    selected = lines if max_lines == 0 else lines[:max_lines]
    return " ".join(selected) or None


def _load_doc(ep, max_lines: int = 1) -> str | None:  # type: ignore[type-arg]
    """Load *ep* and return up to *max_lines* lines of its docstring, or ``None``."""
    try:
        return _doc_from_obj(ep.load(), max_lines)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not load %r for doc extraction: %s", ep.value, exc)
        return None


# ---------------------------------------------------------------------------
# Tool functions
# ---------------------------------------------------------------------------

def _matches(ep_name: str, query: str) -> bool:
    """Case-insensitive glob when *query* has wildcards, else a substring test."""
    needle = query.lower()
    name = ep_name.lower()
    if any(c in query for c in ("*", "?", "[")):
        return fnmatch.fnmatch(name, needle)
    return needle in name


def _provider(ep) -> dict:
    """The distribution that registered *ep*, as ``{distribution, version}``.

    Worth a column because entry points are deduplicated by distribution name: when two
    copies of a package are on ``sys.path`` only the first answers, and the symptom is a
    group that is short or empty rather than an error. A row whose ``version`` disagrees
    with its siblings' names the copy actually in use.
    """
    dist = getattr(ep, "dist", None)
    if dist is None:
        return {}
    return {"distribution": dist.metadata["Name"] or "", "version": dist.version}


def list_plugins(group: str = "", query: str = "") -> dict:
    """What is installed: the extension groups, or the plugins in/matching one.

    Args:
        group: Restrict to one entry-point group, e.g. ``"robovast.variation_types"``.
        query: Match entry-point names case-insensitively — a substring, or a glob when
            it contains ``*``/``?``.

    Returns:
        With neither argument, the group catalog: ``{groups, total}``, each
        ``{group, description, loader_module, plugins}`` (a count). Otherwise
        ``{plugins, total}``, each ``{group, name, class, doc, distribution, version}``
        — ``doc`` being the docstring's first line, and ``distribution``/``version`` the
        package that registered it. Use ``get_plugin_details`` for a plugin's parameters.
    """
    if not group and not query:
        return {
            "groups": [{"group": g, "description": m["description"],
                        "loader_module": m["loader_module"],
                        "plugins": len(list(entry_points(group=g)))}
                       for g, m in _PLUGIN_GROUPS.items()],
            "total": len(_PLUGIN_GROUPS),
        }
    if group and group not in _PLUGIN_GROUPS:
        return {"error": f"unknown plugin group {group!r}; known groups: "
                         f"{', '.join(sorted(_PLUGIN_GROUPS))}"}

    records = []
    for grp in [group] if group else _PLUGIN_GROUPS:
        for ep in entry_points(group=grp):
            if query and not _matches(ep.name, query):
                continue
            record = {"group": grp, "name": ep.name, "class": ep.value,
                      "doc": _load_doc(ep), **_provider(ep)}
            if grp == _PANEL_TYPES_GROUP:
                record["surface"] = _panel_surface(ep)
            records.append(record)
    records.sort(key=lambda r: (r["group"], r["name"]))
    return {"plugins": records, "total": len(records)}


def get_plugin_details(group: str, name: str, limit: int = 0) -> dict:
    """One plugin's docstring and — where it declares one — its parameter schema.

    ``parameters`` is the only place a variation type's or search strategy's accepted
    fields are visible: they dispatch by plugin name, so ``get_config_schema`` shows the
    block only as a generic object. Read this before authoring that block.

    Args:
        group: Extension group, e.g. ``"robovast.postprocessing_commands"``.
        name: Exact entry-point name, e.g. ``"FloorplanGeneration"``.
        limit: Maximum non-blank docstring lines; ``0`` = the whole docstring.

    Returns:
        ``{group, name, class, doc[, parameters]}`` where each parameter is
        ``{name, type, required, default}``; or ``{error}``.
    """
    matches = [ep for ep in entry_points(group=group) if ep.name == name]
    if not matches:
        return {"error": f"No plugin '{name}' found in group '{group}'."}
    ep = matches[0]
    try:
        obj = ep.load()
    except Exception as exc:  # noqa: BLE001 - a broken plugin must not raise here
        logger.debug("Could not load %r for details: %s", ep.value, exc)
        obj = None

    details = {
        "group": group,
        "name": ep.name,
        "class": ep.value,
        "doc": _doc_from_obj(obj, limit) if obj is not None else None,
    }
    if group == _PANEL_TYPES_GROUP:
        details["surface"] = _panel_surface(ep)
    parameters = schema_from_object(obj) if obj is not None else None
    if parameters is not None:
        details["parameters"] = parameters
    return details


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------

_TOOLS = [
    list_plugins,
    get_plugin_details,
]


class PluginMetadataPlugin:
    name = "plugin_metadata"

    def register(self, mcp: FastMCP) -> None:
        for fn in _TOOLS:
            mcp.tool()(fn)
