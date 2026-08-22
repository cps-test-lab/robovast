#!/usr/bin/env python3
# Copyright (C) 2026 Frederik Pasch
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

"""Share provider plugin loading, and building the configured one from the environment."""

import os
from importlib.metadata import entry_points

from .base import BaseShareProvider

__all__ = ["BaseShareProvider", "load_share_provider_plugins", "share_type_configured",
           "load_provider_from_env"]


def load_share_provider_plugins() -> dict[str, type[BaseShareProvider]]:
    """Load all registered share provider plugins.

    Discovers plugins registered under the ``robovast.share_providers``
    entry-point group.  Each plugin must be a subclass of
    :class:`~robovast.execution.share_providers.base.BaseShareProvider`.

    Returns:
        dict mapping share-type name (e.g. ``"nextcloud"`` ) to
        the provider class.
    """
    plugins: dict[str, type[BaseShareProvider]] = {}
    eps = entry_points(group="robovast.share_providers")
    for ep in eps:
        try:
            provider_class = ep.load()
            plugins[ep.name] = provider_class
        except Exception as exc:  # pylint: disable=broad-except
            import logging  # pylint: disable=import-outside-toplevel
            logging.getLogger(__name__).warning(
                "Failed to load share provider plugin '%s': %s", ep.name, exc
            )
    return plugins


def share_type_configured() -> bool:
    """``True`` when a share provider is configured in the environment."""
    return bool(os.environ.get("ROBOVAST_SHARE_TYPE", "").strip())


def load_provider_from_env(overrides: "dict | None" = None):
    """Instantiate the configured share provider from the environment, or ``None``.

    Here rather than in the cluster lane, which is where it used to live: the service
    itself now talks to the share (importing a campaign from it, listing it for the web
    UI), and core must never import a lane -- that edge is what would make the
    dependency graph cyclic.

    Args:
        overrides: Optional ``{ENV_VAR: value}`` applied to ``os.environ`` before the
            provider is built -- used to supply corrected credentials without
            relaunching.

    Returns:
        A :class:`~robovast.execution.share_providers.base.BaseShareProvider`, or ``None``
        when ``ROBOVAST_SHARE_TYPE`` is unset.

    Raises:
        ValueError: the configured share type has no registered provider.
        click.UsageError: required provider env vars are missing (from the constructor).
    """
    if overrides:
        for key, value in overrides.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = str(value)

    share_type = os.environ.get("ROBOVAST_SHARE_TYPE", "").strip()
    if not share_type:
        return None

    providers = load_share_provider_plugins()
    if share_type not in providers:
        available = ", ".join(sorted(providers)) or "(none installed)"
        raise ValueError(
            f"Unknown share type '{share_type}'. Available providers: {available}")
    return providers[share_type]()
