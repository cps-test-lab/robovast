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

import inspect
import logging
from importlib.metadata import entry_points
from typing import Dict, List, Tuple

logger = logging.getLogger(__name__)


def _validate_variation_class(name: str, cls: type) -> List[str]:
    """Return a list of validation error strings for *cls*.

    An empty list means the class is valid.
    """
    # Import here to avoid circular imports at module level
    from .base_variation import Variation  # pylint: disable=import-outside-toplevel

    errors: List[str] = []

    if not inspect.isclass(cls):
        errors.append(f"'{name}' is not a class (got {type(cls).__name__})")
        return errors  # further checks are meaningless

    if not issubclass(cls, Variation):
        errors.append(f"'{name}' ({cls.__qualname__}) is not a subclass of Variation")

    if not callable(getattr(cls, "variation", None)):
        errors.append(f"'{name}' does not have a callable 'variation' method")
    elif cls.variation is Variation.variation:
        errors.append(f"'{name}' does not override the 'variation' method")

    return errors


def load_variation_classes() -> Dict[str, type]:
    """Load and validate variation classes from the ``robovast.variation_types`` entry-point group.

    Invalid plugins (import errors or failed validation) are skipped with a
    warning so that the remaining plugins continue to work.
    """
    classes: Dict[str, type] = {}
    try:
        eps = entry_points(group="robovast.variation_types")
        for ep in eps:
            try:
                cls = ep.load()
            except Exception as e:  # pylint: disable=broad-except
                logger.warning("Failed to load variation plugin '%s': %s", ep.name, e)
                continue

            errors = _validate_variation_class(ep.name, cls)
            if errors:
                for err in errors:
                    logger.warning("Invalid variation plugin: %s", err)
                continue

            classes[ep.name] = cls
            logger.debug("Loaded variation plugin '%s' (%s)", ep.name, cls.__qualname__)
    except Exception:  # pylint: disable=broad-except
        pass
    return classes


def validate_variation_plugins() -> List[Tuple[str, List[str]]]:
    """Check all registered variation plugins and return a validation report.

    Returns:
        List of ``(plugin_name, errors)`` tuples.  ``errors`` is an empty list
        for valid plugins and a non-empty list of error strings for invalid ones.
    """
    report: List[Tuple[str, List[str]]] = []
    try:
        eps = entry_points(group="robovast.variation_types")
        for ep in eps:
            try:
                cls = ep.load()
                errors = _validate_variation_class(ep.name, cls)
            except Exception as e:  # pylint: disable=broad-except
                errors = [f"Import error: {e}"]
            report.append((ep.name, errors))
    except Exception as e:  # pylint: disable=broad-except
        logger.error("Could not enumerate variation plugins: %s", e)
    return report
