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

import logging
import os
from importlib.metadata import entry_points

from .base import BaseShareProvider

__all__ = ["BaseShareProvider", "load_share_provider_plugins", "share_type_configured",
           "load_provider_from_env", "unavailable_share_type_message"]

logger = logging.getLogger(__name__)

#: Why a registered provider could not be loaded, ``entry-point name -> reason``.
#: Written by every :func:`load_share_provider_plugins` scan and read by
#: :func:`unavailable_share_type_message`, so a share type that is registered but broken
#: is not reported as one nobody ever heard of.
_LOAD_ERRORS: dict[str, str] = {}


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
    _LOAD_ERRORS.clear()
    eps = entry_points(group="robovast.share_providers")
    for ep in eps:
        try:
            provider_class = ep.load()
            plugins[ep.name] = provider_class
        except Exception as exc:  # pylint: disable=broad-except
            _LOAD_ERRORS[ep.name] = str(exc)
            logger.warning(
                "Failed to load share provider plugin '%s': %s", ep.name, exc
            )
    return plugins


def unavailable_share_type_message(share_type: str,
                                   providers: dict[str, type[BaseShareProvider]]) -> str:
    """Why *share_type* cannot be used, given the *providers* that did load.

    Every caller that looks a share type up needs this sentence, and "unknown share
    type" is the wrong one for a type that is registered and merely failed to import --
    it sends the reader looking for a typo in a name that is spelled correctly. When the
    last scan recorded a load failure for exactly this name, that failure is the answer.
    """
    reason = _LOAD_ERRORS.get(share_type)
    if reason:
        return (f"Share type '{share_type}' is registered but its provider failed to "
                f"load: {reason}")
    available = ", ".join(sorted(providers)) or "(none installed)"
    return f"Unknown share type '{share_type}'. Available providers: {available}"


def share_type_configured() -> bool:
    """``True`` when a share provider is configured in the environment."""
    return bool(os.environ.get("ROBOVAST_SHARE_TYPE", "").strip())


def load_provider_from_env(overrides: "dict | None" = None):
    """Instantiate the configured share provider from the environment, or ``None``.

    Here rather than in the cluster lane: the service itself talks to the share
    (importing a campaign from it, listing it for the web UI), and core must never
    import a lane -- that edge is what would make the dependency graph cyclic.

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
        raise ValueError(unavailable_share_type_message(share_type, providers))
    return providers[share_type]()
