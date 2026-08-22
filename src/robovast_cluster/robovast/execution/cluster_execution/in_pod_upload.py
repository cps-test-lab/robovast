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
them, with optional *overrides* supplied per call -- it lives in core
(:mod:`robovast.execution.share_providers`) and is re-exported here, because the service
resolves the same provider for an import and cannot import this lane.
"""

import logging

logger = logging.getLogger(__name__)


# Both re-exported from core rather than implemented here: the *service* now talks to the
# share too (importing a campaign, listing it for the web UI), and it cannot import a lane.
# One factory, wherever the caller runs.
from robovast.execution.share_providers import (  # noqa: E402  pylint: disable=wrong-import-position
    load_provider_from_env, share_type_configured)

__all__ = ["share_type_configured", "load_provider_from_env", "verify_share_access"]


def verify_share_access(provider) -> None:
    """Run the provider's pre-flight credential check (raises on failure)."""
    logger.info("Verifying share credentials (%s) before starting the campaign...",
                provider.SHARE_TYPE)
    provider.verify_access()
    logger.info("Share credentials OK.")
