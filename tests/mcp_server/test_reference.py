# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the reference MCP tools."""

import types

import pytest

from robovast.mcp_server.plugins.reference import get_config_schema


def test_config_schema_name_carries_naming_constraint():
    schema = get_config_schema()
    name_prop = schema["$defs"]["ConfigurationConfig"]["properties"]["name"]
    desc = name_prop.get("description", "").lower()
    assert "lowercase" in desc
    assert "underscore" in desc


def test_an_unknown_cli_command_is_an_error_result_naming_what_is_there():
    """Raised, this reaches an MCP client as a protocol failure -- which reads as a broken
    server rather than as a misspelled argument. And a refusal that does not name the
    alternatives leaves the caller guessing at a tree it cannot see."""
    from robovast.mcp_server.plugins.reference import get_cli_help
    result = get_cli_help("nosuchcommand")
    assert "nosuchcommand" in result["error"]
    assert "campaign" in result["error"]  # what 'vast' does offer

    nested = get_cli_help("campaign nosuchverb")
    assert "vast campaign" in nested["error"]
    assert "status" in nested["error"]


def test_a_command_asked_for_a_subcommand_says_it_is_not_a_group():
    from robovast.mcp_server.plugins.reference import get_cli_help
    assert "not a group" in get_cli_help("doctor extra")["error"]


def test_a_real_command_still_returns_its_help():
    from robovast.mcp_server.plugins.reference import get_cli_help
    result = get_cli_help("campaign wait")
    assert result["command"] == "campaign wait"
    assert "Usage" in result["help"]


def test_the_bare_listing_is_the_groups_and_not_every_leaf():
    """A caller that wants one area should not be charged for the whole tree, and one that
    is hunting a particular verb should be searching -- so the free listing is the map."""
    from robovast.mcp_server.plugins.reference import get_cli_help
    paths = [e["command"] for e in get_cli_help()["commands"]]
    assert "vast campaign" in paths and "vast cluster" in paths
    assert "vast campaign log" not in paths  # groups only, no leaves


def test_search_finds_a_command_by_a_word_from_its_help():
    """The question the tree listing was being read for: "is there a command for X". A
    keyword that appears only in the one-line help still has to find it, because a caller
    who knew the command's name would not be searching."""
    from robovast.mcp_server.plugins.reference import get_cli_help
    found = [e["command"] for e in get_cli_help(search="postprocessing")["commands"]]
    assert "vast campaign postprocess" in found


def test_search_terms_are_required_together():
    """OR over two ordinary words returns most of the tree, which is the listing the search
    exists to avoid."""
    from robovast.mcp_server.plugins.reference import get_cli_help
    both = {e["command"] for e in get_cli_help(search="campaign log")["commands"]}
    assert "vast campaign log" in both
    assert "vast campaign stop" not in both


def _broken_entry_point(name):
    def load():
        raise ImportError("no module named pkg")
    return types.SimpleNamespace(name=name, value=f"pkg.{name}:group", load=load,
                                 dist=types.SimpleNamespace(name="some-dist"))


@pytest.fixture
def fresh_cli_tree(monkeypatch):
    """The command tree is loaded once per process; each test here loads its own."""
    from robovast.mcp_server.plugins import reference
    monkeypatch.setattr(reference, "_CLI_FAILURES", None)


def test_an_installed_cli_plugin_that_fails_to_load_is_an_error_not_a_shorter_tree(
        monkeypatch, fresh_cli_tree):  # pylint: disable=redefined-outer-name,unused-argument
    """A registered entry point exists only where its distribution is installed, so one
    that cannot load is a broken install. Answering from the rest of the tree would list,
    search and refuse as though that group did not exist -- a wrong answer that looks right."""
    from robovast.client import cli as cli_module
    from robovast.mcp_server.plugins.reference import get_cli_help
    installed = list(cli_module.entry_points(group="robovast.cli_plugins"))
    monkeypatch.setattr(cli_module, "entry_points",
                        lambda group: [*installed, _broken_entry_point("broken")])
    for answer in (get_cli_help(), get_cli_help(search="campaign"),
                   get_cli_help("campaign status"), get_cli_help("broken")):
        assert "'broken'" in answer["error"]
        assert "some-dist" in answer["error"] and "ImportError" in answer["error"]


def test_every_installed_cli_plugin_loads():
    """What is not installed registers no entry point and is simply absent, so a healthy
    install reports no failure at all."""
    from robovast.client import cli as cli_module
    assert cli_module.load_plugins() == {}


def test_a_listed_verb_that_fails_to_load_is_an_error_naming_it(monkeypatch, fresh_cli_tree):  # pylint: disable=redefined-outer-name,unused-argument
    """``vast cluster`` lists its operator verbs from entry-point metadata; one listed but
    unloadable must not be dropped from a search, nor refused as an unknown name."""
    from robovast.client import cluster_cli
    from robovast.mcp_server.plugins.reference import get_cli_help
    group = cluster_cli.cluster
    listed = group._plugins()  # pylint: disable=protected-access
    monkeypatch.setattr(group, "_plugins",
                        lambda: {**listed, "broken": _broken_entry_point("broken")})
    monkeypatch.setattr(group, "load_failures", {})
    for answer in (get_cli_help(search="cluster"), get_cli_help("cluster broken")):
        assert "vast cluster 'broken'" in answer["error"]
        assert "some-dist" in answer["error"] and "could not be loaded" in answer["error"]
