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

"""Shared metadata lookups for the MCP result-browsing plugins.

Reading and listing files is **not** here: it is the service's job now, behind one
address space (:mod:`robovast.common.file_address`), so that the cluster can answer a
read with a single object fetch instead of a client paging a local copy it does not
have. What remains are lookups into a campaign's ``metadata.yaml``.
"""

from pathlib import Path
from typing import Any

import yaml
from robovast.mcp_server import results_resolver

_metadata_cache: dict[Path, dict[str, Any]] = {}


def read_campaign_metadata(campaign_path: Path) -> dict[str, Any]:
    """Read and cache ``metadata.yaml`` from a campaign directory.

    The parsed result is kept in memory so subsequent calls for the same
    campaign are free.

    Args:
        campaign_path: Path to the ``campaign-<id>`` directory.

    Returns:
        Parsed metadata dictionary (empty dict when file is absent).
    """
    key = campaign_path.resolve()
    if key not in _metadata_cache:
        path = campaign_path / "metadata.yaml"
        if path.exists():
            _metadata_cache[key] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        else:
            _metadata_cache[key] = {}
    return _metadata_cache[key]


def _get_config_by_identifier_or_name(
    campaign_id: str, config_identifier_or_name: str,
) -> dict | None:
    """Find a configuration entry in the campaign *metadata.yaml*.

    Searches the ``configurations`` list first by ``config_identifier``,
    then by ``config-name``.  Returns the matching entry dict, or ``None``
    when no match is found or the file is absent.
    """
    campaign_path = results_resolver.resolve_campaign_path(campaign_id)
    data = read_campaign_metadata(campaign_path)
    configs = data.get("configurations", [])
    for c in configs:
        if str(c.get("config_identifier", "")) == config_identifier_or_name or \
                str(c.get("name", "")) == config_identifier_or_name:
            return c
    return None
