#!/usr/bin/env python3

import logging
import sys
import traceback

import click

from robovast.client.project_config import get_project_config


def handle_cli_exception(e: Exception) -> None:
    """Print a command's failure and exit 1; the traceback goes to debug logging.

    A clean user error (``include_traceback = False``, e.g.
    :class:`~robovast.common.errors.CampaignConfigError`) is printed on its own —
    its message is self-contained and actionable. Anything else gets its exception
    type and a pointer to the traceback, because messages like ``[Errno 2] No such
    file or directory: 'x'`` say nothing about what kind of failure this was or
    where to look next.

    Args:
        e: The exception to handle
    """
    logging.debug(f"Full traceback:\n{traceback.format_exc()}")
    message = str(e) or e.__class__.__name__
    unexpected = getattr(e, "include_traceback", True)
    click.echo(f"Error: {e.__class__.__name__}: {message}" if unexpected
               else f"Error: {message}", err=True)
    if unexpected:
        click.echo("Re-run with 'vast -l DEBUG ...' for the full traceback.", err=True)
    sys.exit(1)


__all__ = [
    'get_project_config',
    'handle_cli_exception'
]
