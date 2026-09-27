# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Every ``vast`` verb with ``--json`` names the resolved service on stderr under it.

Its stdout is read by a program, so the ``Target:`` line must not land there. Each verb says
so by passing its flag to ``echo_target(err=)``; this finds every verb that takes the flag
in the root group and in each group the client attaches to it, so a new one cannot print its
target on stdout."""

import ast
import inspect
import textwrap

import click
import pytest

from robovast.client import campaign_cli, container_cli, service_cli
from robovast.client.cli import cli

#: The root group, and the groups the client attaches to it through its entry points, which
#: a checkout on ``PYTHONPATH`` does not register.
_GROUPS = {"": cli, "campaign": campaign_cli.campaign, "service": service_cli.service,
           "container": container_cli.container}


def _commands(group, path=()):
    ctx = click.Context(group)
    for name in group.list_commands(ctx):
        command = group.get_command(ctx, name)
        if isinstance(command, click.Group):
            yield from _commands(command, path + (name,))
        else:
            yield " ".join(path + (name,)), command


def _json_verbs():
    seen = {}
    for name, group in _GROUPS.items():
        for path, command in _commands(group, (name,) if name else ()):
            if any(param.name == "as_json" for param in command.params):
                seen[path] = command
    return [pytest.param(command, id=path) for path, command in sorted(seen.items())]


def _target_calls(fn):
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    return [node for node in ast.walk(tree) if isinstance(node, ast.Call)
            and ast.unparse(node.func) in ("_echo_target", "echo_target")]


@pytest.mark.parametrize("command", _json_verbs())
def test_a_json_verb_names_its_target_on_stderr(command):
    for call in _target_calls(inspect.unwrap(command.callback)):
        err = next((kw.value for kw in call.keywords if kw.arg == "err"), None)
        assert err is not None and ast.unparse(err) == "as_json", ast.unparse(call)
