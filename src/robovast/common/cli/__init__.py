#!/usr/bin/env python3

import logging
import sys
import traceback

import click

from .project_config import get_project_config


def handle_cli_exception(e: Exception) -> None:
    """Handle CLI exceptions with debug traceback logging.

    Args:
        e: The exception to handle
    """
    logging.debug(f"Full traceback:\n{traceback.format_exc()}")
    click.echo(f"Error: {e}", err=True)
    sys.exit(1)


__all__ = [
    'get_project_config',
    'handle_cli_exception'
]
