# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""How a ``vast`` command reports a failure.

In the client layer because the root command group is, and every verb any distribution
attaches uses it -- a client-only install must be able to fail properly, which is not a
capability it can be missing.
"""

import logging
import sys
import traceback

import click

from robovast.client.status import failure_detail


def handle_cli_exception(e: Exception) -> None:
    """Print a command's failure and exit 1; the full traceback goes to debug logging.

    A clean user error (``include_traceback = False``, e.g.
    :class:`~robovast.common.errors.CampaignConfigError`) is printed on its own --
    its message is self-contained and actionable. Anything else is a bug, and gets its
    exception type and the tail of its traceback, rendered by
    :func:`~robovast.client.status.failure_detail` the way every other surface records
    one: messages like ``[Errno 2] No such file or directory: 'x'`` say nothing about
    what kind of failure this was or where it happened, and the frames are in hand.

    Args:
        e: The exception to handle
    """
    logging.debug("Full traceback:\n%s", traceback.format_exc())
    unexpected = getattr(e, "include_traceback", True)
    click.echo(f"Error: {e.__class__.__name__}: {failure_detail(e)}" if unexpected
               else f"Error: {failure_detail(e)}", err=True)
    sys.exit(1)
