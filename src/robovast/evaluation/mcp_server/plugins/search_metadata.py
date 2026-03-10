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

"""MCP plugin for cross-cutting search of metadata.

Provides tools for filtering configurations and runs across campaigns.
"""

import logging
import re
from datetime import datetime
from typing import Literal, TypedDict

from mcp.server.fastmcp import FastMCP

from ..plugin_common import _iter_all_configs

logger = logging.getLogger(__name__)


# -- Filter types ------------------------------------------------------------

class EqFilter(TypedDict):
    """Match a scenario parameter by exact string value (``type="eq"``).

    The actual value is coerced to ``str`` before comparison.

    ``path`` uses dot notation with optional list indexing:
    ``"a.b.c"`` (nested keys), ``"items[0]"`` (specific index),
    ``"items[*]"`` (wildcard — passes if ANY element matches).
    Examples: ``"start_pose.orientation.yaw"``,
    ``"waypoints[*].label"``, ``"wheel_speeds[2]"``.
    """
    type: Literal["eq"]
    path: str
    value: str


class RangeFilter(TypedDict):
    """Match a scenario parameter by numeric range (``type="range"``), inclusive.

    ``min`` and ``max`` are both optional; omit to leave the bound open.
    When the path resolves to a list (via ``[*]``), passes if ANY element
    falls within the range.

    ``path`` uses dot notation with optional list indexing.
    Examples: ``"start_pose.position.x"``, ``"lidar.ranges[*]"``.
    """
    type: Literal["range"]
    path: str
    min: float | None
    max: float | None


class SpatialFilter(TypedDict):
    """Match a scenario parameter by spatial constraint.

    ``path`` must resolve to a dict (or list of dicts via ``[*]``) with
    ``x`` / ``y`` (and ``z`` for 3-D) fields.  Passes if ANY element
    satisfies the constraint.

    ``type`` controls the shape:

    - ``"boundingbox2d"``: ``bounds`` keys ``x_min``, ``x_max``,
      ``y_min``, ``y_max``.
    - ``"boundingbox3d"``: same plus ``z_min``, ``z_max``.
    - ``"radius"``: ``bounds`` keys ``x``, ``y``, ``radius``.

    ``path`` uses dot notation with optional list indexing.
    Examples: ``"start_pose.position"``, ``"waypoints[*]"``.
    """
    type: Literal["boundingbox2d", "boundingbox3d", "radius"]
    path: str
    bounds: dict


Filter = EqFilter | RangeFilter | SpatialFilter


# -- Path resolution ---------------------------------------------------------

_MISSING = object()  # sentinel for absent path
_SEGMENT_RE = re.compile(r'^([^\[]*)(?:\[(\d+|\*)\])?$')


def _split_path(path: str) -> list[tuple[str | None, str | int | None]]:
    """Parse *path* into ``(key, index)`` tuples.

    Each dot-separated segment is split into an optional dict key and an
    optional index.  Index is ``None`` (no bracket), ``"*"`` (wildcard),
    or an ``int`` (specific position).
    """
    result = []
    for seg in path.split("."):
        m = _SEGMENT_RE.fullmatch(seg)
        if not m:
            result.append((seg, None))
            continue
        key: str | None = m.group(1) or None
        raw = m.group(2)
        idx: str | int | None = None if raw is None else ("*" if raw == "*" else int(raw))
        result.append((key, idx))
    return result


def _resolve_segments(cur, segments: list):
    """Yield every value reachable by *segments* from *cur*."""
    if not segments:
        yield cur
        return
    key, idx = segments[0]
    rest = segments[1:]

    if key is not None:
        if not isinstance(cur, dict) or key not in cur:
            return
        cur = cur[key]

    if idx is None:
        yield from _resolve_segments(cur, rest)
    elif idx == "*":
        if not isinstance(cur, list):
            return
        for item in cur:
            yield from _resolve_segments(item, rest)
    else:
        if not isinstance(cur, list) or idx >= len(cur):
            return
        yield from _resolve_segments(cur[idx], rest)


def _resolve_path(data: dict, path: str):
    """Resolve *path* against *data*.

    Returns a single value, a list of values (when ``[*]`` is present),
    or ``_MISSING`` when the path cannot be resolved.
    """
    segments = _split_path(path)
    has_wildcard = any(idx == "*" for _, idx in segments)
    values = list(_resolve_segments(data, segments))
    if has_wildcard:
        return values  # may be empty list — callers check length
    return values[0] if values else _MISSING


# -- Filter matchers ---------------------------------------------------------


def _apply_filter(config: dict, f: Filter) -> bool:
    """Dispatch a single filter to the appropriate matcher."""
    kind = f["type"]
    if kind == "eq":
        return _match_param(config, f)
    if kind == "range":
        return _match_range(config, f)
    return _match_spatial(config, f)  # boundingbox2d / boundingbox3d / radius


def _match_param(config: dict, f: EqFilter) -> bool:
    val = _resolve_path(config, f["path"])
    if val is _MISSING:
        return False
    items = val if isinstance(val, list) else [val]
    target = str(f["value"])
    return any(str(v) == target for v in items)


def _match_range(config: dict, f: RangeFilter) -> bool:
    val = _resolve_path(config, f["path"])
    if val is _MISSING:
        return False
    items = val if isinstance(val, list) else [val]
    lo = f.get("min")
    hi = f.get("max")
    for v in items:
        try:
            num = float(v)
        except (TypeError, ValueError):
            continue
        if lo is not None and num < lo:
            continue
        if hi is not None and num > hi:
            continue
        return True
    return False


def _match_spatial(config: dict, f: SpatialFilter) -> bool:
    val = _resolve_path(config, f["path"])
    if val is _MISSING:
        return False
    points = val if isinstance(val, list) else [val]
    bounds = f.get("bounds", {})
    kind = f["type"]
    for point in points:
        if not isinstance(point, dict):
            continue
        try:
            px = float(point["x"])
            py = float(point["y"])
        except (KeyError, TypeError, ValueError):
            continue
        if kind == "boundingbox2d":
            if bounds["x_min"] <= px <= bounds["x_max"] and bounds["y_min"] <= py <= bounds["y_max"]:
                return True
        elif kind == "boundingbox3d":
            try:
                pz = float(point["z"])
            except (KeyError, TypeError, ValueError):
                continue
            if (bounds["x_min"] <= px <= bounds["x_max"]
                    and bounds["y_min"] <= py <= bounds["y_max"]
                    and bounds["z_min"] <= pz <= bounds["z_max"]):
                return True
        elif kind == "radius":
            dx = px - bounds["x"]
            dy = py - bounds["y"]
            if dx * dx + dy * dy <= bounds["radius"] ** 2:
                return True
    return False


def _compute_duration(start_time: str | None, end_time: str | None) -> float | None:
    """Return duration in seconds between two ISO timestamps, or None."""
    if not start_time or not end_time:
        return None
    try:
        dt_start = datetime.fromisoformat(start_time).replace(tzinfo=None)
        dt_end = datetime.fromisoformat(end_time).replace(tzinfo=None)
        return (dt_end - dt_start).total_seconds()
    except (ValueError, TypeError):
        return None


def _success_rate(test_results: list[dict]) -> float | None:
    """Return success rate as a float 0.0–1.0, or None if no runs."""
    if not test_results:
        return None
    successes = sum(
        1 for r in test_results
        if str(r.get("success", "")).lower() == "true"
    )
    return successes / len(test_results)


# -- Tool functions ----------------------------------------------------------


def search_configurations(
    campaign_id: str | None = None,
    filters: list[Filter] | None = None,
    min_success_rate: float | None = None,
    max_success_rate: float | None = None,
    config_identifier: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> list[dict]:
    """Search configurations using structured filters and success rate.

    All supplied filters are ANDed together.  Omit any filter to skip it.
    When *campaign_id* is omitted all campaigns are searched.

    Each filter is a dict with a ``"type"`` discriminator plus type-specific
    fields.  Path syntax: ``"a.b.c"`` (nested keys), ``"items[0]"`` (index),
    ``"items[*]"`` (wildcard — passes if ANY element matches).

    Filter types:

    - ``"eq"``            – exact string match: ``{"type": "eq", "path": "...", "value": "..."}``
    - ``"range"``         – numeric range:      ``{"type": "range", "path": "...", "min": float|null, "max": float|null}``
    - ``"boundingbox2d"`` – 2-D bounding box:   ``{"type": "boundingbox2d", "path": "...", "bounds": {"x_min":…, "x_max":…, "y_min":…, "y_max":…}}``
    - ``"boundingbox3d"`` – 3-D bounding box:   same plus ``"z_min"``/``"z_max"``
    - ``"radius"``        – circle/sphere:       ``{"type": "radius", "path": "...", "bounds": {"x":…, "y":…, "radius":…}}``

    Examples::

        # Exact match: configs with zero Gaussian noise
        filters=[{"type": "eq", "path": "laserscan_gaussian_noise_std_deviation", "value": "0.0"}]

        # Exact match on nested field
        filters=[{"type": "eq", "path": "start_pose.orientation.yaw", "value": "0.0"}]

        # Numeric range: start position x between 0 and 5
        filters=[{"type": "range", "path": "start_pose.position.x", "min": 0.0, "max": 5.0}]

        # Spatial bounding box: start pose inside a rectangle
        filters=[{"type": "boundingbox2d", "path": "start_pose.position",
                  "bounds": {"x_min": -5.0, "x_max": 5.0, "y_min": 0.0, "y_max": 10.0}}]

        # Spatial radius: goal pose within 3 m of a point
        filters=[{"type": "radius", "path": "goal_pose.position",
                  "bounds": {"x": 0.0, "y": 9.0, "radius": 3.0}}]

        # Any waypoint within a radius (wildcard list)
        filters=[{"type": "radius", "path": "waypoints[*]",
                  "bounds": {"x": 1.0, "y": 2.0, "radius": 1.5}}]

        # Combined: low-noise, all-failed, start inside a bounding box
        filters=[
            {"type": "eq",   "path": "laserscan_gaussian_noise_std_deviation", "value": "0.0"},
            {"type": "boundingbox2d", "path": "start_pose.position",
             "bounds": {"x_min": 0.0, "x_max": 10.0, "y_min": 0.0, "y_max": 10.0}},
        ],
        max_success_rate=0.0

    Args:
        campaign_id: Restrict search to this campaign (optional).
        filters: List of filter dicts, all ANDed.  Each has a ``"type"``
            field that selects ``"eq"``, ``"range"``, ``"boundingbox2d"``,
            ``"boundingbox3d"``, or ``"radius"``.
        min_success_rate: Minimum success rate 0.0–1.0 (optional).
        max_success_rate: Maximum success rate 0.0–1.0 (optional).
        config_identifier: Substring match on config identifier (optional).
        limit: Maximum number of results (default 20).
        offset: Number of results to skip (default 0).
    """
    results: list[dict] = []
    for cid, c in _iter_all_configs(campaign_id):
        config = c.get("config", {}) or {}

        if filters:
            if not all(_apply_filter(config, f) for f in filters):
                continue

        if config_identifier is not None:
            if config_identifier not in str(c.get("config_identifier", "")):
                continue

        test_results = c.get("test_results", [])
        rate = _success_rate(test_results)

        if min_success_rate is not None and (rate is None or rate < min_success_rate):
            continue
        if max_success_rate is not None and (rate is None or rate > max_success_rate):
            continue

        results.append({
            "campaign_id": cid,
            "name": c.get("name"),
            "identifier": c.get("config_identifier"),
            "scenario_params": config,
            "num_runs": len(test_results),
            "success_rate": rate,
        })

    return results[offset : offset + limit]


def search_runs(
    campaign_id: str | None = None,
    configuration_id: str | None = None,
    success: bool | None = None,
    min_duration_s: float | None = None,
    max_duration_s: float | None = None,
    instance_type: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> list[dict]:
    """Search runs by success status, duration, or instance type.

    Filters are ANDed together.  Omit a filter to skip it.
    When *campaign_id* is omitted all campaigns are searched.

    Args:
        campaign_id: Restrict search to this campaign (optional).
        configuration_id: Restrict to this configuration name or
            identifier (optional).
        success: Filter by pass (True) or fail (False) (optional).
        min_duration_s: Minimum run duration in seconds (optional).
        max_duration_s: Maximum run duration in seconds (optional).
        instance_type: Substring match on instance type (optional).
        limit: Maximum number of results (default 20).
        offset: Number of results to skip (default 0).
    """
    results: list[dict] = []
    for cid, c in _iter_all_configs(campaign_id):
        config_name = c.get("name", "")
        config_ident = str(c.get("config_identifier", ""))

        if configuration_id is not None:
            if configuration_id not in (config_name, config_ident):
                continue

        for tr in c.get("test_results", []):
            run_dir = tr.get("dir", "")
            run_num = run_dir.split("/")[-1] if "/" in run_dir else run_dir

            passed = tr.get("success")
            if passed is not None:
                passed = str(passed).lower() == "true"

            if success is not None and passed != success:
                continue

            duration = _compute_duration(tr.get("start_time"), tr.get("end_time"))

            if min_duration_s is not None:
                if duration is None or duration < min_duration_s:
                    continue
            if max_duration_s is not None:
                if duration is None or duration > max_duration_s:
                    continue

            sysinfo = tr.get("sysinfo", {})
            inst_type = sysinfo.get("instance_type", "")

            if instance_type is not None:
                if instance_type not in inst_type:
                    continue

            results.append({
                "campaign_id": cid,
                "config_name": config_name,
                "run": run_num,
                "success": passed,
                "duration_s": duration,
                "instance_type": inst_type,
                "start_time": tr.get("start_time"),
            })

    return results[offset : offset + limit]



# -- Plugin class ------------------------------------------------------------

_TOOLS = [
    search_configurations,
    search_runs,
]


class SearchMetadataPlugin:
    """Cross-cutting search and comparison tools for campaign metadata."""

    name = "search_metadata"

    def register(self, mcp: FastMCP) -> None:
        """Register all tool functions with the MCP server."""
        for fn in _TOOLS:
            mcp.tool()(fn)
