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
from robovast.mcp_server.server import create_server

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
    # The read/list pairs, collapsed onto one tool each with an empty argument meaning
    # "all of them". Two tools over one object cost two schemas in every request and a
    # round trip to learn the name the second one needs.
    "list_cli_commands",       # -> get_cli_help(command="")
    "list_docs",               # -> search_docs(query="", page="")
    "list_examples",           # -> get_example(name="")
    "list_workspaces_info",    # -> list_workspaces(workspace_id="")
    "get_workspace",           # -> list_workspaces(workspace_id="")
    "list_plugin_groups",      # -> list_plugins()
    "search_plugin",           # -> list_plugins(query=...)
    "list_running_campaigns",  # -> list_campaigns(running_only=True)
    "campaign_data_status",    # -> describe_campaign_data(preflight_only=True)
    "cleanup_campaign_data",   # -> delete_campaign(data_only=True)
    # Nav: the stats variants became a flag on the tool that reads the same data, and the
    # data-model blurb moved into the docstrings of the tools it described.
    "nav_describe_data_model",
    "nav_get_planned_path", "nav_get_path",
    "nav_get_trajectory_stats",        # -> nav_get_trajectory(stats_only=True)
    "nav_get_map_occupancy_stats",     # -> nav_get_map_info(occupancy=True)
    "display_simulation_screenshot",   # -> get_simulation_screenshot
    "resource_usage",                  # -> get_resource_usage
    # Built, then deliberately dropped: waiting for a campaign is `vast exec wait`, a
    # command a caller can background, because a campaign can run for days and a blocking
    # tool call would occupy its caller for the whole of it. Listed here for the usual
    # reason — a retired name left in a docstring is one an LLM will try to call — and
    # because this one reads so plausibly that it invites reintroduction.
    "wait_for_campaign",
]

#: Commands that do not exist on the ``vast`` CLI. A tool that tells an LLM to run one
#: sends it somewhere there is nothing to run: four nav error messages named
#: ``vast analysis postprocess``, which has never been a command.
#:
#: Kept as a denylist *in addition to* the derived check below, for names that read
#: plausibly enough to invite reintroduction even after they stop appearing.
_FORBIDDEN_COMMANDS = [
    "vast analysis ",
]


def _real_command_paths() -> set:
    """Every command path the assembled ``vast`` CLI actually offers.

    Derived from the click tree with all plugins loaded, so it is whatever this install
    really provides -- the point being that a denylist can only catch a command somebody
    remembered to add to it.
    """
    from robovast.client.cli import cli, load_plugins  # pylint: disable=import-outside-toplevel
    load_plugins()

    import click  # pylint: disable=import-outside-toplevel

    def walk(group, prefix):
        yield " ".join(prefix)
        if not isinstance(group, click.Group):
            return
        ctx = click.Context(group)
        # `list_commands`/`get_command`, not `.commands`: the exec group registers its
        # lane subgroups lazily, so the dict is empty until something asks. Reading it
        # directly reported `vast exec cluster` as non-existent.
        for name in group.list_commands(ctx):
            sub = group.get_command(ctx, name)
            if sub is not None:
                yield from walk(sub, prefix + (name,))

    return set(walk(cli, ("vast",)))


#: A `vast ...` invocation in prose: the words after `vast` that are plainly command
#: names. Stops at the first token that is a flag, a placeholder, a path, or punctuation.
_INVOCATION = re.compile(r"\bvast((?:\s+[a-z][a-z0-9-]*)+)")


def _registered_plugin_modules():
    """Every module a ``robovast.mcp_plugins`` entry point resolves to.

    The guards below used to scan ``mcp_server/plugins/`` only, which is one
    distribution's worth of plugins. The ``nav`` plugin ships from ``robovast_nav`` and
    was therefore unguarded — it accumulated a tool function dropped from ``_TOOLS`` but
    left in the file, and four error messages naming a ``vast`` command that does not
    exist. Resolving the modules from the registry covers whatever is installed.
    """
    mods = {}
    for ep in entry_points(group="robovast.mcp_plugins"):
        module_path = ep.value.split(":", 1)[0]
        mods[module_path] = importlib.import_module(module_path)
    for f in _PLUGINS_DIR.glob("*.py"):
        if f.stem != "__init__":
            name = f"robovast.mcp_server.plugins.{f.stem}"
            mods.setdefault(name, importlib.import_module(name))
    return mods


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
    """Every public function in a registered plugin module must be a registered tool.

    A public function dropped from ``_TOOLS`` but left in the file is dead code an
    LLM can't call but a reader assumes exists."""
    orphans = {}
    for path, mod in _registered_plugin_modules().items():
        tools = getattr(mod, "_TOOLS", None)
        if tools is None:
            continue  # e.g. prompts registers a prompt, not tools
        tool_names = {t.__name__ for t in tools}
        for name, obj in vars(mod).items():
            if (inspect.isfunction(obj) and obj.__module__ == mod.__name__
                    and not name.startswith("_") and name not in tool_names):
                orphans.setdefault(path, []).append(name)
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


def _llm_facing_text() -> dict[str, str]:
    """Everything an LLM reads from this server, keyed by where it came from.

    Deliberately not "every occurrence in the source": ``get_workspace``,
    ``resource_usage`` and ``cleanup_campaign_data`` are live ``RobovastInterface``
    methods that the tools call. Forbidding the *identifier* would forbid the code; what
    must not survive is the retired name in text an LLM is given and will try to call.
    """
    text = {"server instructions": create_server().instructions or ""}
    for plugin, tools in load_registered_tool_details().items():
        for tool in tools:
            text[f"tool {tool['name']}"] = tool["name"] + "\n" + (tool["summary"] or "")
    for path, mod in _registered_plugin_modules().items():
        for name, obj in vars(mod).items():
            if inspect.isfunction(obj) and obj.__module__ == mod.__name__ and obj.__doc__:
                text[f"{path}.{name} docstring"] = obj.__doc__
    from robovast.mcp_server.plugins import prompts
    text["analyze prompt"] = prompts._SYSTEM_PROMPT
    text["run prompt"] = prompts._RUN_PROMPT
    # Only the tool-surface page. ``developer_guide.rst`` and ``architecture.rst``
    # document ``RobovastInterface``, where ``get_workspace`` and
    # ``cleanup_campaign_data`` are current method names — correct there, and a name
    # that survives as a method is not a name an LLM is being offered as a tool.
    text["mcp.rst"] = (_DOCS_DIR / "mcp.rst").read_text(encoding="utf-8")

    # Strings a tool *returns* to an LLM, not just ones it documents. `next_step` is the
    # sharpest example: it is the command an agent runs verbatim after start_campaign,
    # and it kept naming `vast exec wait` for a whole refactor because every guard here
    # read docstrings and this is a runtime return value.
    from robovast.mcp_server.plugins.execution import \
        _wait_next_step  # pylint: disable=import-outside-toplevel
    text["start_campaign next_step"] = _wait_next_step("<campaign-id>")
    return text


@pytest.mark.parametrize("phantom", _FORBIDDEN_NAMES)
def test_no_phantom_tool_names_in_llm_facing_text(phantom):
    """A retired tool name in text an LLM reads is a name it will try to call.

    Matched on word boundaries, so ``resource_usage`` does not flag its own replacement
    ``get_resource_usage``, nor ``nav_get_path`` flag ``nav_get_path_deviation``.
    """
    pattern = re.compile(rf"\b{re.escape(phantom)}\b")
    hits = [where for where, text in _llm_facing_text().items() if pattern.search(text)]
    assert not hits, f"retired tool name {phantom!r} still referenced in: {hits}"


@pytest.mark.parametrize("phantom", _FORBIDDEN_COMMANDS)
def test_no_phantom_cli_commands_in_llm_facing_text(phantom):
    """A tool that tells an LLM to run a command sends it somewhere; the command must exist."""
    hits = [where for where, text in _llm_facing_text().items() if phantom in text]
    assert not hits, f"non-existent CLI command {phantom!r} referenced in: {hits}"


def test_every_vast_invocation_in_llm_facing_text_resolves():
    """Every ``vast ...`` an LLM is told to run must be a command that exists.

    The denylist above cannot do this: it only catches names somebody thought to add.
    ``vast exec wait`` proved the gap -- waiting moved to the top level, the MCP's
    ``next_step`` kept handing back the old spelling, and every guard stayed green while
    the tool sent agents at ``Error: No such command 'wait'``. Precisely the step where a
    campaign gets lost, which is what makes this worth deriving rather than listing.
    """
    real = _real_command_paths()
    bad = {}
    for where, text in _llm_facing_text().items():
        for match in _INVOCATION.finditer(text):
            words = match.group(1).split()
            # Longest prefix that resolves wins: trailing words are arguments
            # (`vast wait <id>`), and only a *first* word that resolves to nothing is a
            # phantom command rather than an argument to a real one.
            if " ".join(["vast", words[0]]) not in real:
                bad.setdefault(where, set()).add(f"vast {words[0]}")
                continue
            path = ["vast"]
            for word in words:
                if " ".join(path + [word]) not in real:
                    break
                path.append(word)
            # A second word that is neither a subcommand nor plausibly an argument.
            if len(words) > 1 and len(path) == 2 and re.fullmatch(r"[a-z-]+", words[1]):
                candidate = f"vast {words[0]} {words[1]}"
                if candidate not in real and any(
                        p.startswith(f"vast {words[0]} ") for p in real):
                    bad.setdefault(where, set()).add(candidate)
    assert not bad, "\n".join(
        f"{where}: {sorted(cmds)}" for where, cmds in sorted(bad.items()))


# SQL identifiers the prompt legitimately backticks. They collide with the verb
# heuristic below ("run_view" and "run_id" both start with the verb ``run``) without
# being tools at all, so they are named here rather than weakening the heuristic — the
# point of which is to catch a *tool* name that does not exist.
_NON_TOOL_IDENTIFIERS = {
    "run_view", "config_view", "run_id", "config_name", "run_data",
    "config_json", "params_json", "objectives_json", "measures_json", "sysinfo_json",
    "duration_s", "postprocessing_steps", "table_name",
}


@pytest.mark.parametrize("source", ["_SYSTEM_PROMPT", "_RUN_PROMPT", "instructions"])
def test_prompt_references_only_real_tools(source):
    """Every tool-shaped name backticked in text an LLM is given must resolve to a tool.

    Catches the phantom ``query_run_data_table`` class of bug — a name the model will
    happily call once, fail on, and route around."""
    from robovast.mcp_server.plugins import prompts
    text = (create_server().instructions if source == "instructions"
            else getattr(prompts, source))
    registered = _registered_tool_names()
    # tool-shaped: verb-prefixed snake_case, the convention tools follow.
    verbs = ("get", "list", "query", "describe", "search", "inspect", "draw",
             "display", "create", "write", "edit", "read", "delete", "update",
             "run", "start", "stop", "validate", "preview", "init", "build")
    candidates = set(re.findall(r"`([a-z]+_[a-z0-9_]+)`", text))
    tool_like = {c for c in candidates
                 if c.split("_", 1)[0] in verbs and c not in _NON_TOOL_IDENTIFIERS}
    unresolved = {c for c in tool_like if c not in registered}
    assert not unresolved, (
        f"{source} references non-registered tools: {sorted(unresolved)}")


def test_the_server_says_it_runs_experiments_before_any_tool_is_read():
    """The instructions are the only text read before a tool is chosen.

    They used to say the server "provides access to the results created by RoboVAST" —
    true, and the reason agents ran experiments by hand on the host and came here only to
    read files. An archive is not offered as a place to run anything.
    """
    instructions = (create_server().instructions or "").lower()
    assert "run" in instructions and "not on this host" in instructions
    assert "start_campaign" in instructions
    # And the refusal has to be part of the framing, not only of the error string.
    assert "stop and report" in instructions


@pytest.mark.parametrize("source", ["_RUN_PROMPT", "instructions"])
def test_checking_the_image_is_part_of_the_loop_an_agent_is_given(source):
    """A capability missing from the loop is a capability nobody uses.

    ``exec_in_container`` and ``build_experiment_image`` existed, were documented, and had
    good descriptions — and the loop went ``validate → preview → usage → start_campaign``,
    so an agent following it reached ``start_campaign`` with an unverified image and paid
    a full campaign to learn a package was missing. That is the same mistake this file
    already records about itself: the instructions once introduced the server as an
    archive, and the whole execution half went unused.
    """
    from robovast.mcp_server.plugins import prompts
    text = (create_server().instructions if source == "instructions"
            else prompts._RUN_PROMPT) or ""
    assert "exec_in_container" in text, (
        f"{source} never mentions it, so the cheap check is invisible where it matters")
    assert "build_experiment_image" in text
    # Ordered before the step that launches: after it, the cycle this saves is already
    # paid. Anchored to the *last* mention, since both texts name `start_campaign` up
    # front in the "run it here, not on this host" framing, well before the loop.
    assert text.index("exec_in_container") < text.rindex("start_campaign"), (
        f"{source} mentions the check only after the campaign is launched")


#: One name per concept, one concept per name. Each entry was a divergence: the nav tools
#: took ``campaign``/``config``/``run`` where the other 51 took the long forms; the
#: "how many to return" argument had eight names (``max_rows``, ``max_points``,
#: ``max_configs``, ``max_lines``, ``lines``, plus ``limit``); and ``config_path`` meant a
#: workspace-relative path in the execution tools and an absolute filesystem path in the
#: authoring ones — the same word for two address spaces.
#:
#: ``tail`` (last N) and ``top`` (top N patterns) are deliberately *not* ``limit``: they
#: are different operations, and collapsing them would hide that.
_BANNED_PARAMETERS = {
    "campaign": "campaign_id",
    "config": "config_name",
    "run": "run_id",
    "max_rows": "limit",
    "max_points": "limit",
    "max_configs": "limit",
    "max_lines": "limit",
    "lines": "limit",
}


def _tool_parameters() -> dict[str, dict]:
    import asyncio

    async def _tools():
        return await create_server().list_tools()

    return {t.name: (getattr(t, "parameters", None) or {}).get("properties", {})
            for t in asyncio.run(_tools())}


def test_tools_share_one_parameter_vocabulary():
    """A concept must have the same argument name everywhere it appears."""
    offenders = {}
    for tool, props in _tool_parameters().items():
        for param in props:
            if param in _BANNED_PARAMETERS:
                offenders.setdefault(tool, []).append(
                    f"{param} -> {_BANNED_PARAMETERS[param]}")
    assert not offenders, f"tools using a retired parameter name: {offenders}"


def test_every_tool_returns_a_dict_or_an_image():
    """One error convention: ``{"error": …}`` in the result dict.

    Four coexisted. ``list_docs``/``list_examples`` were typed ``list[dict] | str`` and
    returned a bare *sentence* when the directory was missing; ``list_plugins`` returned
    a one-element list holding an error dict, so a listing's shape doubled as a refusal;
    and the nav tools raised, which reaches an MCP client as a broken server rather than
    as an answer. A caller could not write one branch that handled failure.

    The two image tools are the stated exception — an ``Image`` has nowhere to put an
    ``error`` key, so they raise, and say so in their docstrings.
    """
    from fastmcp.utilities.types import Image
    wrong = {}
    for path, mod in _registered_plugin_modules().items():
        for fn in getattr(mod, "_TOOLS", []):
            annotation = inspect.signature(fn).return_annotation
            if annotation not in (dict, "dict", Image, "Image"):
                wrong[f"{path}.{fn.__name__}"] = str(annotation)
    assert not wrong, f"tools whose return type is neither dict nor Image: {wrong}"


def test_a_shared_parameter_name_keeps_one_type():
    """``limit`` must not be an integer on one tool and a string on another."""
    types: dict[str, set] = {}
    for props in _tool_parameters().values():
        for param, spec in props.items():
            if spec.get("type"):
                types.setdefault(param, set()).add(spec["type"])
    mixed = {p: sorted(t) for p, t in types.items() if len(t) > 1}
    assert not mixed, f"parameters with more than one type across tools: {mixed}"


#: Budget for the tool surface, in approximate tokens. Every description and schema is
#: injected into the model's context on **every** request, so this is a recurring cost
#: paid per turn, not a one-off. It stood at ~14.3k across 61 tools; the ceiling is set
#: above the current figure with room for a tool or two, and is meant to force a
#: deliberate decision — compress something, or merge something — rather than to drift.
#:
#: Raised 11_000 → 11_300 when ``start_campaign`` gained ``from_campaign`` (relaunch a
#: campaign from its own recorded config and pinned image). The alternative was a
#: ``retrigger_campaign`` tool of its own, which would have cost more; the parameter's own
#: text was compressed from ~164 to ~122 tokens first, and most of what is left is the
#: JSON-schema entry rather than prose.
#:
#: Raised 11_300 → 11_600 for ``search_run_logs``, which answers the one log question no
#: stream can — "which of these runs logged this, and did they fail?" — by searching the
#: merged ``run_log`` table across runs and campaigns. Paid for first, in this order: the
#: tool's own text went from ~885 to ~475 tokens (four parameters dropped outright, since
#: ``query_campaign_data_sql`` reaches the same columns for the rare question that needs
#: them), and the fat this note used to point at, ``start_campaign.show_gui``, was
#: compressed from ~199 tokens to ~90. Net cost of the new capability: ~230.
#:
#: Raised 11_600 → 12_600 to absorb accumulated drift: the surface had already reached ~12_113
#: across 52 tools, so the ceiling was being enforced retroactively rather than deciding anything.
#: **This raise is the exception to the rule above, and is not a precedent**: unlike the two
#: before it, nothing was compressed to pay for it and it buys no new capability. It restores the
#: ceiling's purpose — sitting just above the real figure, with room for a tool or two — instead
#: of failing every run for growth nobody chose.
#:
#: Where the fat is, measured: ``start_campaign`` (~769), ``exec_in_container`` (~700) and
#: ``get_campaign_log`` (~644) are 17% of the whole surface between them, and all three document
#: behaviour that has since become more uniform. The next capability should be paid for out of
#: those rather than by moving this number again.
#:
#: Raised 12_600 → 13_000 for ``wait_for_image_build``. Two thirds of that is not the tool:
#: the surface already stood at ~12_701 when this was written, so ~101 was drift the old
#: ceiling was failing on before anything here was added. Paid for as that note asked —
#: ``start_campaign`` went from ~769 to ~670 tokens (−99) and is barely the largest tool now.
#:
#: A sibling ``wait_for_campaign`` was built and then deliberately **not** kept, which is the
#: more useful precedent. Both tools answered the same defect — an operation returns while its
#: work continues, and nothing waits for it, so an agent reads one status and ends its turn.
#: But a campaign can run for days, and waiting for it *inside a tool call* occupies the caller
#: for the whole of it. That wait belongs in a shell command a harness can background
#: (``vast exec wait``, over the same ``execution.campaign_wait`` loop), which costs no surface
#: at all. A build is minutes and always has work behind it in the same turn, so blocking there
#: costs nothing and needs no background plumbing — hence one tool, not two.
#:
#: The general rule this leaves: **if the wait can outlive a turn, it is not a tool.**
#:
#: Raised 13_000 → 13_500 for the image catalog's four tools (``list_scenario_actions``,
#: ``get_scenario_action_details``, ``list_roqsim_plugins``, ``get_roqsim_plugin_details``
#: — 245 tokens together). Unlike the raise above, this one was **not** paid for by
#: compressing anything, which is a deliberate exception and worth recording as such: the
#: plugin had been written but never registered as an entry point, so its tools were
#: absent from the surface while ``test_every_plugin_class_is_registered`` failed. Fixing
#: that registration added all four at once and put the surface 240 over.
#:
#: Where the fat is now: ``start_campaign`` (~670), ``exec_in_container`` (~654) and
#: ``get_campaign_log`` (~578). The next capability should still be paid for out of those
#: — this raise bought ~260 of headroom, not a new habit.
_SURFACE_TOKEN_BUDGET = 13_500


def test_the_tool_surface_stays_within_its_token_budget():
    import asyncio
    import json

    async def _tools():
        return await create_server().list_tools()

    total = 0
    per_tool = {}
    for tool in asyncio.run(_tools()):
        chars = len(tool.description or "") + len(
            json.dumps(getattr(tool, "parameters", None) or {}))
        per_tool[tool.name] = chars // 4
        total += chars // 4
    worst = sorted(per_tool.items(), key=lambda kv: -kv[1])[:5]
    assert total <= _SURFACE_TOKEN_BUDGET, (
        f"tool surface is ~{total} tokens, over the {_SURFACE_TOKEN_BUDGET} budget. "
        f"Largest: {worst}. Compress a description or merge two tools — do not just "
        "raise the budget.")
