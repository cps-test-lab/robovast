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

"""Load and pre-flight the configured share provider, in-process on the driver.

The actual delivery is a **streamed** upload: the campaign is tarred + gzipped on the
fly from the driver's scratch straight into ``provider.upload_archive_stream`` (see
:meth:`KubernetesBackend.share_campaign`), so no compressed copy ever lands on disk.
This module stays generic and just resolves the provider and checks its credentials.

Share credentials come from the service's own environment (its Deployment env), so
they are already present in ``os.environ`` here. :func:`load_provider_from_env` reads
them, with optional *overrides* supplied per call.
"""

import logging
import os

logger = logging.getLogger(__name__)


def share_type_configured() -> bool:
    """Return ``True`` if a share provider is configured in the environment."""
    return bool(os.environ.get("ROBOVAST_SHARE_TYPE", "").strip())


def load_provider_from_env(overrides: dict | None = None):
    """Instantiate the configured share provider from the environment.

    Args:
        overrides: Optional ``{ENV_VAR: value}`` applied to ``os.environ`` before
            the provider is built — used by the retrigger command to supply
            corrected credentials without relaunching the controller.

    Returns:
        An instantiated :class:`~robovast.execution.share_providers.base.BaseShareProvider`, or
        ``None`` when ``ROBOVAST_SHARE_TYPE`` is unset.

    Raises:
        ValueError: when the configured share type has no registered provider.
        click.UsageError: when required provider env vars are missing (raised by
            the provider constructor).
    """
    from robovast.execution.share_providers import \
        load_share_provider_plugins  # pylint: disable=import-outside-toplevel

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
            f"Unknown share type '{share_type}'. Available providers: {available}"
        )
    return providers[share_type]()


def verify_share_access(provider) -> None:
    """Run the provider's pre-flight credential check (raises on failure)."""
    logger.info("Verifying share credentials (%s) before starting the campaign...",
                provider.SHARE_TYPE)
    provider.verify_access()
    logger.info("Share credentials OK.")
