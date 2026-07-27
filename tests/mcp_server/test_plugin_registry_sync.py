# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Drift guards: keep the MCP tool surface, the plugin registry, and the docs in
sync so the reference cannot silently diverge from what the server exposes.

Each test targets a real defect class found while consolidating the surface:
unregistered plugins, dead public functions left after ``_TOOLS`` pruning, and
phantom tool names in docstrings/prompts/docs.
"""

import importlib
import inspect
import pathlib
import re
from importlib.metadata import entry_points

import pytest

from robovast.mcp_server.registry import load_registered_tool_details

_PLUGINS_DIR = pathlib.Path(__file__).resolve().parents[2] / \
    "src" / "robovast" / "mcp_server" / "plugins"
_DOCS_DIR = pathlib.Path(__file__).resolve().parents[2] / "docs"

# Phantom tool names that previously leaked into docstrings/prompts/docs. They
# name tools that never existed (or were renamed away); guard against their return.
_FORBIDDEN_NAMES = [
    "list_run_data_tables", "query_run_data_table", "query_run_data_tables",
    "inspect_run_data_table", "query_run_log",
    # Removed deliberately: the service has one project binding (``workspace_id``),
    # so there is nothing for an MCP-side ``vast init`` to bind. ``.robovast_project``
    # remains a CLI-only concept -- see LocalTransport._resolve_project.
    "init_project",
    # The per-scope file tools, collapsed into read_file/list_files/write_file/
    # edit_file/delete_file over one address space (``/results/<campaign>/<path>`` and
    # ``/sources/<workspace>/<path>``). Listed here because a retired tool name left in
    # a docstring or a doc page is one an LLM will try to call -- so the migration is
    # described by what the tools *did*, never by their names.
    "get_campaign_scenario",
    "list_campaign_run_files", "get_campaign_run_file",
    "list_campaign_transient_files", "get_campaign_transient_file",
    "list_configuration_transient_files", "get_configuration_transient_file",
    "list_configuration_config_files",
    "list_run_additional_output_files", "get_run_output_file",
    "read_project_file", "list_project_files", "write_project_file",
    "edit_project_file", "delete_project_file",
    # The metadata.yaml views, collapsed onto read-only SQL. Each was a hand-rolled
    # reader of a file only postprocessing writes, so all of them answered "run
    # postprocessing first" for campaigns whose results were already in campaign.db.
    # Their questions are now one WHERE clause on ``run_view`` (or, for the campaign's
    # configurations, a directory listing — SQL knows only configs that produced runs).
    # Listed here because a retired tool name left in a docstring or a doc page is one an
    # LLM will try to call: the replacements are described by what they answer, never by
    # the name of the tool that used to answer it.
    "get_configuration_summary", "get_configuration_scenario_parameter",
    "get_configuration_variations", "get_run_details", "get_run_sysinfo",
    "list_campaign_configurations", "get_campaign_execution_details",
    "get_campaign_postprocessing_details",
]


def _plugin_classes_in_source():
    """Return ``{ClassName}`` for every ``*Plugin`` class defined under plugins/."""
    names = set()
    for f in _PLUGINS_DIR.glob("*.py"):
        if f.stem == "__init__":
            continue
        mod = importlib.import_module(f"robovast.mcp_server.plugins.{f.stem}")
        for name, obj in vars(mod).items():
            if inspect.isclass(obj) and obj.__module__ == mod.__name__ \
                    and name.endswith("Plugin"):
                names.add(name)
    return names


def _registered_plugin_classes():
    """Return ``{ClassName}`` for every class behind a ``robovast.mcp_plugins`` EP."""
    return {ep.value.rsplit(":", 1)[-1] for ep in entry_points(group="robovast.mcp_plugins")}


def test_every_plugin_class_is_registered():
    """A ``*Plugin`` class that isn't an entry point is dead weight the server
    never loads (this is exactly how ``search_metadata`` rotted)."""
    unregistered = _plugin_classes_in_source() - _registered_plugin_classes()
    assert not unregistered, (
        f"Plugin classes not registered as robovast.mcp_plugins entry points: "
        f"{sorted(unregistered)}")


def test_no_orphan_public_functions_in_plugins():
    """Every public function in a plugin module must be a registered tool.

    A public function dropped from ``_TOOLS`` but left in the file is dead code an
    LLM can't call but a reader assumes exists."""
    orphans = {}
    for f in _PLUGINS_DIR.glob("*.py"):
        if f.stem == "__init__":
            continue
        mod = importlib.import_module(f"robovast.mcp_server.plugins.{f.stem}")
        tools = getattr(mod, "_TOOLS", None)
        if tools is None:
            continue  # e.g. prompts registers a prompt, not tools
        tool_names = {t.__name__ for t in tools}
        for name, obj in vars(mod).items():
            if (inspect.isfunction(obj) and obj.__module__ == mod.__name__
                    and not name.startswith("_") and name not in tool_names):
                orphans.setdefault(f.stem, []).append(name)
    assert not orphans, f"Public plugin functions not in _TOOLS (dead code): {orphans}"


def test_docs_use_registry_driven_directive():
    """mcp.rst must render tools via the argument-free ``.. mcp-tools::`` directive
    (which reads the registry) rather than hand-listed per-module tables."""
    text = (_DOCS_DIR / "mcp.rst").read_text(encoding="utf-8")
    assert re.search(r"^\.\. mcp-tools::\s*$", text, re.MULTILINE), \
        "mcp.rst should invoke the argument-free `.. mcp-tools::` directive"
    # And the old per-module form must not creep back.
    assert "mcp-tools:: robovast.mcp_server.plugins" not in text


def test_registry_directive_covers_every_registered_plugin():
    """The docs source (registry-driven) must expose every registered plugin that
    has tools — so a newly added plugin cannot go undocumented."""
    details = load_registered_tool_details()
    documented = {p for p, tools in details.items() if tools}
    # Every lifecycle phase is present in the structure the directive renders, so a phase
    # cannot go undocumented after the modules were re-cut along these lines.
    for phase in ("files", "authoring", "execution", "results", "results_lifecycle",
                  "reference"):
        assert phase in documented, f"{phase} must be documented"
    # No plugin with tools is silently empty.
    assert all(details[p] for p in documented)


def _registered_tool_names():
    names = set()
    for tools in load_registered_tool_details().values():
        names.update(t["name"] for t in tools)
    return names


@pytest.mark.parametrize("phantom", _FORBIDDEN_NAMES)
def test_no_phantom_tool_names_in_source_or_docs(phantom):
    """Known phantom tool names must not appear in source or docs."""
    hits = []
    for base, pattern in ((_PLUGINS_DIR.parent, "**/*.py"), (_DOCS_DIR, "**/*.rst")):
        for f in base.glob(pattern):
            if "_build" in f.parts:
                continue
            if phantom in f.read_text(encoding="utf-8", errors="ignore"):
                hits.append(str(f.relative_to(base.parents[0] if pattern.endswith('py') else base)))
    assert not hits, f"phantom tool name {phantom!r} still referenced in: {hits}"


# SQL identifiers the prompt legitimately backticks. They collide with the verb
# heuristic below ("run_view" and "run_id" both start with the verb ``run``) without
# being tools at all, so they are named here rather than weakening the heuristic — the
# point of which is to catch a *tool* name that does not exist.
_NON_TOOL_IDENTIFIERS = {
    "run_view", "config_view", "run_id", "config_name", "run_data",
    "config_json", "params_json", "objectives_json", "measures_json", "sysinfo_json",
    "duration_s", "postprocessing_steps", "table_name",
}


def test_prompt_references_only_real_tools():
    """Every tool-shaped name backticked in the analyze prompt must resolve to a
    registered tool (catches the phantom `query_run_data_table` class of bug)."""
    from robovast.mcp_server.plugins import prompts
    registered = _registered_tool_names()
    # tool-shaped: verb-prefixed snake_case, the convention tools follow.
    verbs = ("get", "list", "query", "describe", "search", "inspect", "draw",
             "display", "create", "write", "edit", "read", "delete", "update",
             "run", "start", "stop", "validate", "preview", "init")
    candidates = set(re.findall(r"`([a-z]+_[a-z0-9_]+)`", prompts._SYSTEM_PROMPT))
    tool_like = {c for c in candidates
                 if c.split("_", 1)[0] in verbs and c not in _NON_TOOL_IDENTIFIERS}
    unresolved = {c for c in tool_like if c not in registered}
    assert not unresolved, (
        f"analyze prompt references non-registered tools: {sorted(unresolved)}")
