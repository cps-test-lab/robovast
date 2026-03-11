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

"""Shared helpers for MCP result-browsing plugins."""

from pathlib import Path
from typing import Any

import yaml
from robovast.evaluation.mcp_server import results_resolver

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


def _is_binary(path: Path) -> bool:
    """Return True if *path* looks like a binary file."""
    try:
        with open(path, "rb") as f:
            chunk = f.read(8192)
        return b"\x00" in chunk
    except OSError:
        return True


def _read_text_paginated(path: Path, lines: int = 100, offset: int = 0) -> dict:
    """Read *lines* text lines starting at *offset* from *path*.

    Returns a dict with ``content``, ``total_lines``, ``returned_lines``,
    ``offset``, and ``file_name``.
    """
    if _is_binary(path):
        return {
            "file_name": path.name,
            "error": "Binary file — content cannot be displayed.",
        }
    text = path.read_text(encoding="utf-8", errors="replace")
    all_lines = text.splitlines()
    total = len(all_lines)
    selected = all_lines[offset : offset + lines]
    return {
        "file_name": path.name,
        "total_lines": total,
        "returned_lines": len(selected),
        "offset": offset,
        "content": "\n".join(selected),
    }


def _list_files_relative(directory: Path) -> list[str]:
    """Return sorted list of file paths relative to *directory*."""
    if not directory.is_dir():
        return []
    return sorted(str(f.relative_to(directory)) for f in directory.rglob("*") if f.is_file())


def _iter_all_configs(
    campaign_id: str | None = None,
):
    """Yield ``(campaign_id_str, config_entry)`` tuples across campaigns.

    When *campaign_id* is given only that campaign is searched; otherwise
    every campaign in the results directory is visited.
    """
    if campaign_id is not None:
        campaign_path = results_resolver.resolve_campaign_path(campaign_id)
        data = read_campaign_metadata(campaign_path)
        for c in data.get("configurations", []):
            yield campaign_id, c
    else:
        for d in results_resolver.list_campaigns():
            cid = d.name
            data = read_campaign_metadata(d)
            for c in data.get("configurations", []):
                yield cid, c


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
