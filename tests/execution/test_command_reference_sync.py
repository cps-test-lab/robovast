# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Drift guard: the command reference renders every top-level ``vast`` command.

``docs/how_to_run.rst`` is a list of ``sphinx-click`` directives, one per group or verb, so a
command registered without a directive appears on no page and nothing fails. A ``:commands:``
filter hides every verb of its group that it does not name, which is the same drift one
level down.
"""

import pathlib
import re

import click

from robovast.client import cli as cli_module

_PAGE = pathlib.Path(__file__).resolve().parents[2] / "docs" / "how_to_run.rst"


def test_every_top_level_command_has_a_directive():
    cli_module.load_plugins()
    root = cli_module.cli
    ctx = click.Context(root, info_name="vast")
    commands = {}
    for name in root.list_commands(ctx):
        # An alias is the same command object under a second name; one directive covers both.
        commands.setdefault(id(root.get_command(ctx, name)), set()).add(name)
    rendered = set(re.findall(r"^\s+:prog: vast (\S+)\s*$", _PAGE.read_text(encoding="utf-8"),
                              re.M))
    missing = sorted("/".join(sorted(names)) for names in commands.values()
                     if not names & rendered)
    assert not missing, f"no directive in {_PAGE.name} for: {', '.join(missing)}"


def test_no_directive_filters_its_group():
    assert ":commands:" not in _PAGE.read_text(encoding="utf-8"), (
        "a :commands: filter hides the verbs it does not name; render the group whole")
