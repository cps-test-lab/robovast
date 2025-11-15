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
import sys


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
        # Format for WARNING and above (show level)
        warning_format = '%(levelname)s: %(message)s'

        def __init__(self):
            super().__init__()

        def format(self, record):
            # Choose format based on log level
            if record.levelno == logging.DEBUG:
                formatter = logging.Formatter(self.debug_format, datefmt='%Y-%m-%d %H:%M:%S')
            elif record.levelno == logging.INFO:
                formatter = logging.Formatter(self.info_format)
            else:
                formatter = logging.Formatter(self.warning_format)
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


def setup_logging_from_project_config() -> None:
    """Setup logging using the project configuration if available.

    If no project configuration is found, defaults to INFO level.
    """
    try:
        from .cli.project_config import ProjectConfig

        config = ProjectConfig.load()
        if config:
            setup_logging(config.log_level)
        else:
            setup_logging("INFO")
    except Exception:
        # If we can't load the config, just use INFO level
        setup_logging("INFO")
