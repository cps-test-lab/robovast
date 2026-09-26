# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Every command's option names are its own: a short flag a command adds beside the target
options (``-n``, ``-x``) must not be one of theirs, or click resolves it to whichever it saw
first and warns at every invocation."""

from collections import Counter
from importlib.metadata import entry_points

import click

from robovast.client.cli import cli


def _all_commands():
    """Every leaf command: the root group's, and each group or command a distribution
    registers as a ``vast`` plugin, which the root attaches only when it runs."""
    yield from _commands(cli)
    for ep in entry_points(group="robovast.cli_plugins"):
        loaded = ep.load()
        if isinstance(loaded, click.Group):
            yield from _commands(loaded, (ep.name,))
        else:
            yield ep.name, loaded


def _commands(group, prefix=()):
    """Asked the way click asks, so a group that offers verbs from an entry-point group
    (``vast cluster``, ``vast service``) yields those too, not only the ones it declares."""
    ctx = click.Context(group, info_name=prefix[-1] if prefix else "vast")
    for name in group.list_commands(ctx):
        command = group.get_command(ctx, name)
        if command is None:
            continue
        path = prefix + (name,)
        if isinstance(command, click.Group):
            yield from _commands(command, path)
        else:
            yield " ".join(path), command


def test_the_walk_reaches_the_verbs_a_group_attaches_from_entry_points():
    """``vast cluster`` declares no verb of its own; every one of its verbs comes from
    ``robovast-cluster``, and a walk over the declared ones alone would check none."""
    paths = {path for path, _ in _all_commands()}
    assert "cluster setup" in paths
    assert "service upgrade" in paths


def test_no_command_declares_one_option_name_twice():
    clashes = {}
    for path, command in _all_commands():
        names = Counter(opt for param in command.params
                        for opt in param.opts + param.secondary_opts)
        repeated = sorted(name for name, n in names.items() if n > 1)
        if repeated:
            clashes[path] = repeated
    assert not clashes, f"option names declared twice: {clashes}"
