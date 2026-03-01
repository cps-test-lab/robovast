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

"""Share provider plugin loading for cluster upload-to-share."""

from importlib.metadata import entry_points

from .base import BaseShareProvider

__all__ = ["BaseShareProvider", "load_share_provider_plugins"]


def load_share_provider_plugins() -> dict[str, type[BaseShareProvider]]:
    """Load all registered share provider plugins.

    Discovers plugins registered under the ``robovast.share_providers``
    entry-point group.  Each plugin must be a subclass of
    :class:`~robovast.execution.cluster_execution.share_providers.base.BaseShareProvider`.

    Returns:
        dict mapping share-type name (e.g. ``"nextcloud"``, ``"gdrive"``) to
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
