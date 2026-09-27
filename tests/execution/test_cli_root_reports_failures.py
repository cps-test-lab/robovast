# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A ``vast`` verb's failure is reported once, as a message, whichever verb it is.

A verb that reaches a service can fail before it prints anything, and not every verb wraps
its body in a handler. The root group is the one place over all of them, so a failure that
escapes a verb is printed the way every handled one is -- and never as a raw traceback."""

import click
from click.testing import CliRunner

from robovast.client.cli import cli
from robovast.service.interface import ServiceError


def _invoke(command):
    """Run *command* as a verb attached to the root group, the way a plugin's is."""
    cli.add_command(command, name="probe")
    try:
        return CliRunner().invoke(cli, ["probe"])
    finally:
        del cli.commands["probe"]


def test_an_unhandled_bug_is_reported_with_its_type_and_frames():
    @click.command()
    def probe():
        raise KeyError("no such slot")

    result = _invoke(probe)
    assert result.exit_code == 1
    assert result.stderr.startswith("Error: KeyError: 'no such slot'\n")
    assert "in probe" in result.stderr
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_an_unhandled_refusal_is_its_message_alone():
    @click.command()
    def probe():
        raise ServiceError(404, "no workspace named 'x'")

    result = _invoke(probe)
    assert result.exit_code == 1
    assert result.stderr == "Error: no workspace named 'x'\n"


def test_a_verb_that_handled_its_own_refusal_prints_it_once():
    """The refusal raised before a verb's body runs -- no service answers -- reaches the
    verb's own broad handler first; it must come out as click renders it, not as a bug."""
    from robovast.client.errors import handle_cli_exception

    @click.command()
    def probe():
        try:
            raise click.ClickException("no robovast-service found")
        except Exception as e:  # noqa: BLE001 - the pattern every verb uses
            handle_cli_exception(e)

    result = _invoke(probe)
    assert result.exit_code == 1
    assert result.stderr == "Error: no robovast-service found\n"


def test_a_usage_error_keeps_clicks_exit_code():
    @click.command()
    @click.argument("thing")
    def probe(thing):
        del thing

    result = _invoke(probe)
    assert result.exit_code == 2
    assert "Missing argument" in result.stderr
