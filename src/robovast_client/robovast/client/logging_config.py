#!/usr/bin/env python3
# Copyright (C) 2025 Frederik Pasch
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions
# and limitations under the License.
#
# SPDX-License-Identifier: Apache-2.0

"""Logging configuration for RoboVAST."""

import logging
import os
import sys
import threading


def setup_logging(log_level: str = "INFO") -> None:
    """Configure logging for RoboVAST.

    Args:
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
    """
    # Convert string to logging level
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)

    # Create a custom formatter that adjusts based on log level
    class CustomFormatter(logging.Formatter):
        """Custom formatter that shows different formats based on log level."""

        # Format for DEBUG level (verbose)
        debug_format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        # Format for INFO level (clean)
        info_format = '%(message)s'
        # Format for WARNING / ERROR / CRITICAL (show level with icon)
        warning_format  = '⚠️  %(levelname)s: %(message)s'
        error_format    = '🔴 %(levelname)s: %(message)s'
        critical_format = '💀 %(levelname)s: %(message)s'

        def __init__(self):
            super().__init__()

        def format(self, record):
            # Choose format based on log level
            if record.levelno == logging.DEBUG:
                formatter = logging.Formatter(self.debug_format, datefmt='%Y-%m-%d %H:%M:%S')
            elif record.levelno == logging.INFO:
                formatter = logging.Formatter(self.info_format)
            elif record.levelno == logging.WARNING:
                formatter = logging.Formatter(self.warning_format)
            elif record.levelno == logging.ERROR:
                formatter = logging.Formatter(self.error_format)
            else:  # CRITICAL and above
                formatter = logging.Formatter(self.critical_format)
            return formatter.format(record)

    # Configure root logger
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(CustomFormatter())

    logging.basicConfig(
        level=numeric_level,
        handlers=[handler]
    )


def get_logger(name: str) -> logging.Logger:
    """Get a logger instance for a module.

    Args:
        name: Name of the module (typically __name__)

    Returns:
        Logger instance
    """
    return logging.getLogger(name)


class _ThreadOwnedFilter(logging.Filter):
    """Pass only records emitted from an owned set of threads.

    The ``robovast`` logger is process-wide, but the service now drives several
    campaigns concurrently as threads in one process. Without this, every
    campaign's records would fan out to *every* campaign's ``controller.log``.
    Seeded with the attaching thread (the campaign's worker); ``owned`` is public
    so a campaign that spawns a logging helper thread can register its ident too.
    """

    def __init__(self, owner_ident: int):
        super().__init__()
        self.owned: set[int] = {owner_ident}

    def filter(self, record: logging.LogRecord) -> bool:
        return record.thread in self.owned


def add_campaign_log_handler(log_path: str,
                             isolate_thread: bool = True) -> logging.Handler:
    """Attach a verbose ``FileHandler`` at *log_path* to the ``robovast`` logger.

    The handler is added to the top-level ``robovast`` logger (not the root
    logger) so it captures every ``robovast.*`` record via propagation while
    leaving noisy third-party libraries (``botocore``, ``urllib3``,
    ``kubernetes``) out of the file. Its level is left at ``NOTSET``, so the
    ``robovast`` logger's effective level gates records — the file mirrors the
    console (INFO by default, DEBUG if configured). Existing handlers are
    untouched, so console output is unaffected.

    Args:
        log_path: Destination file. Parent directories are created if needed.
        isolate_thread: When True (default), only records from the calling thread
            reach this file — so concurrent campaigns in one process (the service)
            keep separate ``controller.log`` files. The attached filter is exposed
            on the handler's ``.filters`` (an :class:`_ThreadOwnedFilter` whose
            ``owned`` set can be extended for helper threads).

    Returns:
        The attached handler, to be passed to :func:`remove_campaign_log_handler`.
    """
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"))
    if isolate_thread:
        handler.addFilter(_ThreadOwnedFilter(threading.get_ident()))
    logging.getLogger("robovast").addHandler(handler)
    return handler


def remove_campaign_log_handler(handler: logging.Handler | None) -> None:
    """Flush, close and detach a handler from :func:`add_campaign_log_handler`."""
    if handler is None:
        return
    try:
        handler.flush()
        handler.close()
    finally:
        logging.getLogger("robovast").removeHandler(handler)


def setup_default_logging() -> None:
    """Set the default log level for a command that did not pass ``--log-level``.

    INFO, always. This used to read a level out of a ``.robovast_project`` found by
    walking up to the filesystem root, so a file in some ancestor directory decided how
    verbose an unrelated command was -- and silently, since nothing said where the level
    came from. ``--log-level`` is the one way to change it.
    """
    setup_logging("INFO")
