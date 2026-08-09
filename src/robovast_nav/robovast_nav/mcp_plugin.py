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

"""MCP plugin for robovast-nav: navigation analysis, environment, and map tools.

Two sources, chosen by what actually holds the fact, and never by what is easiest to
open from here:

**The database.** Trajectories, action feedback and everything derived from them are read
with SQL over the campaign's ``data.db``, exactly as the core ``results`` plugin reads
everything else. Postprocessing already ingests each CSV it produces into a table keyed on
``(config_name, run_id)``, so re-parsing ``poses.csv`` was a second reader of the same
fact — one that only worked when the run happened to sit on this host's disk, and that
pulled a whole file to answer a question about eight numbers.

**Files, through the address space.** Maps, videos and the resolved scenario parameters are
artifacts with no table behind them. They are reached by their ``/results/<campaign_id>/…``
address via the service, not by building a local path: a cluster campaign's durable home is
the object store, and a local-path read reported every one of them as "not found".
"""

from __future__ import annotations

import contextlib
import io
import logging
import math
import tempfile
from pathlib import Path
from typing import Any

import matplotlib
import numpy as np
import yaml
from matplotlib import patches as mpatches
from fastmcp import FastMCP
from fastmcp.utilities.types import Image

from robovast.common.file_address import RESULTS, format_address
from robovast.mcp_server import data_access, service_access

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402  # pylint: disable=wrong-import-position,ungrouped-imports

logger = logging.getLogger(__name__)

#: Ceiling the read-only query layer clamps ``max_rows`` to. Named here so the sampling
#: notice can state the real limit rather than a number that drifts from it.
_QUERY_MAX_ROWS = 5000


class NavDataError(Exception):
    """A nav read that cannot be answered, with a message the caller can act on.

    Every tool converts this to ``{"error": …}``. It exists so the helpers can refuse
    from anywhere without each of them having to know the caller's response shape — and
    so a missing campaign stops arriving at an MCP client as a protocol-level exception.
    """


# ---------------------------------------------------------------------------
# Reaching a campaign: files by address, data by SQL
# ---------------------------------------------------------------------------

def _client():
    """The service when one answers, else an in-process transport over local results."""
    return service_access.client_or_local()


def _address(campaign_id: str, *parts) -> str:
    """The ``/results/<campaign_id>/<path>`` address for *parts*."""
    return format_address(RESULTS, campaign_id,
                          "/".join(str(p).strip("/") for p in parts if str(p)))


def _read_text(campaign_id: str, *parts) -> str:
    """Text of a campaign file, by address."""
    address = _address(campaign_id, *parts)
    try:
        return _client().read_file(address, lines=0).content
    except Exception as e:  # noqa: BLE001
        raise NavDataError(f"could not read {address}: {e}") from e


def _read_yaml(campaign_id: str, *parts) -> dict:
    """A campaign YAML file, parsed."""
    address = _address(campaign_id, *parts)
    try:
        return yaml.safe_load(_read_text(campaign_id, *parts)) or {}
    except yaml.YAMLError as e:
        raise NavDataError(f"could not parse {address}: {e}") from e


def _list_dir(campaign_id: str, *parts) -> list[str]:
    """Entry names in a campaign directory, by address. Directories end in ``/``."""
    address = _address(campaign_id, *parts).rstrip("/") + "/"
    try:
        return list(_client().list_files(address, limit=1000).entries)
    except Exception as e:  # noqa: BLE001
        raise NavDataError(f"could not list {address}: {e}") from e


@contextlib.contextmanager
def _materialized(campaign_id: str, prefix: tuple, names):
    """Copy campaign files into a temp dir and yield it, so path-based readers can run.

    The map visualizer takes a filesystem path, and a cluster campaign has none — its files
    are objects. Fetching the bytes by address and writing them side by side (keeping their
    names, so a map YAML still finds its image) is what lets it work on either backend
    instead of only the local one.
    """
    client = _client()
    with tempfile.TemporaryDirectory(prefix="robovast-nav-") as tmp:
        root = Path(tmp)
        for name in names:
            address = _address(campaign_id, *prefix, name)
            try:
                (root / name).write_bytes(client.read_file_bytes(address))
            except Exception as e:  # noqa: BLE001
                raise NavDataError(f"could not fetch {address}: {e}") from e
        yield root


def _scenario_params(campaign_id: str, config_name: str, *,
                     required: bool = True) -> dict:
    """A configuration's resolved scenario parameters, unwrapped from the scenario name.

    ``required=False`` yields ``{}`` for a configuration that has no ``scenario.config``
    at all, so a caller with a second place to look can go there instead of reporting a
    read failure the user cannot act on.
    """
    try:
        content = _read_yaml(campaign_id, config_name, "_config", "scenario.config")
    except NavDataError:
        if required:
            raise NavDataError(
                f"config {config_name!r} of {campaign_id!r} has no "
                "_config/scenario.config, which is where a configuration's resolved "
                "scenario parameters are recorded. Check the config name against "
                f"list_files('/results/{campaign_id}/').") from None
        return {}
    if isinstance(content, dict) and len(content) == 1:
        content = next(iter(content.values()))
    return content or {}


def _lit(value) -> str:
    """A SQL string literal. The query transports take SQL text, not bound parameters."""
    return "'" + str(value).replace("'", "''") + "'"


def _tables(campaign_id: str) -> dict[str, list[str]]:
    """``{table_name: [column, …]}`` for the campaign's queryable data.

    Consulted before every query so a missing table becomes "postprocessing has not run
    this step" — which names the fix — instead of SQLite's "no such table".
    """
    described = data_access.describe(campaign_id)
    if "error" in described:
        raise NavDataError(described["error"])
    return {t["table"]: [c.split(" ", 1)[0] for c in t.get("columns", [])]
            for t in described.get("tables", []) if t.get("schema") == "main"}


def _require_table(campaign_id: str, name: str, produced_by: str) -> list[str]:
    """The columns of table *name*, or a refusal naming the plugin that would create it."""
    tables = _tables(campaign_id)
    if name not in tables:
        available = ", ".join(sorted(tables)) or "(none)"
        raise NavDataError(
            f"campaign {campaign_id!r} has no {name!r} table, so this cannot be "
            f"answered. It is created by the {produced_by!r} postprocessing step — "
            f"configure it in the .vast and re-run postprocessing with the "
            f"run_postprocessing tool. Tables present: {available}.")
    return tables[name]


def _find_table(campaign_id: str, patterns: tuple, produced_by: str) -> str:
    """The one table whose name matches any of *patterns*, or a refusal listing what is there."""
    import fnmatch  # pylint: disable=import-outside-toplevel
    tables = _tables(campaign_id)
    matches = sorted(t for t in tables
                     if any(fnmatch.fnmatch(t, p) for p in patterns))
    if not matches:
        available = ", ".join(sorted(tables)) or "(none)"
        raise NavDataError(
            f"campaign {campaign_id!r} has no table matching {list(patterns)}, so this "
            f"cannot be answered. Such a table is created by the {produced_by!r} "
            f"postprocessing step — configure it in the .vast and re-run postprocessing "
            f"with the run_postprocessing tool. Tables present: {available}.")
    return matches[0]


def _query(campaign_id: str, sql: str, limit: int = _QUERY_MAX_ROWS) -> list[dict]:
    """Rows of a read-only ``SELECT``, or a refusal carrying the query layer's message."""
    result = data_access.query(campaign_id, sql, limit)
    if "error" in result:
        raise NavDataError(result["error"])
    return result.get("rows") or []


def _stride_sql(inner: str, order_by: str, limit: int) -> str:
    """Wrap *inner* so it returns at most *limit* evenly-spaced rows, plus the true count.

    The sampling happens in the database rather than after transferring everything: a
    30-second run holds several thousand poses, and the caller asked for a couple of
    hundred. ``total_points`` rides along on every row so the reply can state what the
    stride was taken from — a thinned trajectory that does not say so reads as the whole
    one.
    """
    return f"""
        WITH src AS ({inner}),
             idx AS (SELECT *, ROW_NUMBER() OVER (ORDER BY {order_by}) - 1 AS _i,
                            COUNT(*) OVER () AS _n
                     FROM src)
        SELECT * FROM idx
        WHERE _n <= {limit} OR _i % MAX(1, (_n + {limit} - 1) / {limit}) = 0
        ORDER BY _i
        LIMIT {limit}
    """


def _pose_source(campaign_id: str, config_name: str, run_id: int, frame: str) -> str:
    """The ``SELECT`` yielding one run's poses in a frame, typed and named uniformly.

    ``CAST`` is not optional: a ``data.db`` built before typed ingest stores every column
    as TEXT, where ``MAX`` and ``ORDER BY`` compare lexicographically ('10.02' < '9.5')
    and would report a wrong maximum without failing.
    """
    return f"""
        SELECT CAST("timestamp" AS REAL) AS t,
               CAST("position.x" AS REAL) AS x,
               CAST("position.y" AS REAL) AS y,
               CAST("orientation.yaw" AS REAL) AS yaw
        FROM poses
        WHERE config_name = {_lit(config_name)}
          AND CAST(run_id AS INTEGER) = {int(run_id)}
          AND frame = {_lit(frame)}
    """


def _require_poses(campaign_id: str, config_name: str, run_id: int, frame: str) -> None:
    """Refuse early, naming the frames that *do* exist, when the frame filter matches nothing.

    An empty trajectory and a misspelled frame are the same empty result set, and the
    second is by far the more likely — so the frames present are part of the refusal.
    """
    columns = _require_table(campaign_id, "poses", "rosbags_tf_to_csv")
    missing = [c for c in ("timestamp", "position.x", "position.y", "orientation.yaw")
               if c not in columns]
    if missing:
        raise NavDataError(
            f"the 'poses' table of {campaign_id!r} is missing {missing}; it has "
            f"{columns}. This is a different recording than rosbags_tf_to_csv produces.")
    present = _query(campaign_id, f"""
        SELECT DISTINCT frame FROM poses
        WHERE config_name = {_lit(config_name)}
          AND CAST(run_id AS INTEGER) = {int(run_id)}
    """)
    frames = [r["frame"] for r in present]
    if not frames:
        raise NavDataError(
            f"no poses recorded for run {run_id} of config {config_name!r} in "
            f"{campaign_id!r}.")
    if frame not in frames:
        raise NavDataError(
            f"frame {frame!r} was not recorded for run {run_id} of "
            f"{config_name!r}; recorded frames: {', '.join(sorted(frames))}.")


def _point_to_segment_distance(
    px: float, py: float,
    ax: float, ay: float,
    bx: float, by: float,
) -> float:
    """Compute the distance from point (px,py) to line segment (ax,ay)-(bx,by)."""
    dx = bx - ax
    dy = by - ay
    length_sq = dx * dx + dy * dy
    if length_sq == 0:
        return math.sqrt((px - ax) ** 2 + (py - ay) ** 2)

    t = max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / length_sq))
    proj_x = ax + t * dx
    proj_y = ay + t * dy
    return math.sqrt((px - proj_x) ** 2 + (py - proj_y) ** 2)


def _reporting(fn):
    """Turn a :class:`NavDataError` into the ``{"error": …}`` every other MCP tool returns.

    The nav tools used to let a ``ValueError`` escape, which reaches an MCP client as a
    protocol error rather than as an answer — so "this campaign is on the cluster, not
    here" was indistinguishable from the server being broken.
    """
    import functools  # pylint: disable=import-outside-toplevel

    @functools.wraps(fn)
    def _wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except NavDataError as e:
            return {"error": str(e)}
    return _wrapped


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@_reporting
def nav_get_obstacles(campaign_id: str, config_name: str) -> dict:
    """What was in the robot's way? A configuration's static obstacles.

    Args:
        campaign_id: Campaign identifier.
        config_name: Configuration directory name (``list_files`` on the campaign root
            shows them).

    Returns:
        ``{obstacles, total}``, each ``{entity_name, model, x, y, yaw}`` — metres and
        radians — plus ``xacro_arguments`` where the configuration sets them.
        Or ``{error}``.
    """
    # From the configuration's resolved scenario.config, not SQL — and that was checked,
    # not assumed: ``unit.params_json`` (via run_view) records the parameters the campaign
    # *varied*, and ``config_view`` the ``.vast`` as authored, which holds the variation
    # *spec*. Neither holds the resolved object list, so there is no table to query.
    objects = _scenario_params(campaign_id, config_name).get("static_objects") or []

    obstacles = []
    for obj in objects:
        pose = obj.get("spawn_pose", {}) or {}
        entry: dict[str, Any] = {
            "entity_name": obj.get("entity_name"),
            "model": obj.get("model"),
            "x": (pose.get("position") or {}).get("x"),
            "y": (pose.get("position") or {}).get("y"),
            "yaw": (pose.get("orientation") or {}).get("yaw"),
        }
        if "xacro_arguments" in obj:
            entry["xacro_arguments"] = obj["xacro_arguments"]
        obstacles.append(entry)
    return {"obstacles": obstacles, "total": len(obstacles)}


@_reporting
def nav_get_trajectory(
    campaign_id: str,
    config_name: str,
    run_id: int,
    frame: str = "base_link",
    limit: int = 200,
    stats_only: bool = False,
) -> dict:
    """Where did the robot go? One run's trajectory, as points or as a summary.

    From the ``poses`` table (needs ``rosbags_tf_to_csv`` postprocessing).

    Args:
        campaign_id: Campaign identifier.
        config_name: Configuration directory name.
        run_id: Run index within that configuration.
        frame: TF frame (default ``base_link``); an unrecorded frame is refused with the
            list of those that were recorded.
        limit: Maximum points; sampled at an even stride beyond that, with
            ``total_points`` stating the true count.
        stats_only: Return the summary instead of the points. Computed over **every**
            recorded pose regardless of ``limit`` — a statistic from a thinned trajectory
            is a different number that looks like the same one.

    Returns:
        Points: ``{frame, total_points, returned_points, sampled, points}``, each point
        ``{timestamp, x, y, yaw}``. With ``stats_only``: ``{frame, num_points,
        total_distance_m, duration_sec, avg_speed_m_s, max_speed_m_s, start_pose,
        end_pose, bounding_box}`` (poses are ``{x, y, yaw}``, yaw in radians).
        Or ``{error}``.
    """
    _require_poses(campaign_id, config_name, run_id, frame)
    source = _pose_source(campaign_id, config_name, run_id, frame)

    if stats_only:
        rows = _query(campaign_id, f"""
            WITH p AS ({source}),
                 d AS (SELECT t, x, y, yaw,
                              x - LAG(x) OVER w AS dx,
                              y - LAG(y) OVER w AS dy,
                              t - LAG(t) OVER w AS dt,
                              FIRST_VALUE(x)  OVER w AS x0,
                              FIRST_VALUE(y)  OVER w AS y0,
                              FIRST_VALUE(yaw) OVER w AS yaw0,
                              LAST_VALUE(x)  OVER wf AS x1,
                              LAST_VALUE(y)  OVER wf AS y1,
                              LAST_VALUE(yaw) OVER wf AS yaw1
                       FROM p
                       WINDOW w  AS (ORDER BY t),
                              wf AS (ORDER BY t
                                     ROWS BETWEEN UNBOUNDED PRECEDING
                                              AND UNBOUNDED FOLLOWING))
            SELECT COUNT(*)                                        AS num_points,
                   SUM(COALESCE(SQRT(dx*dx + dy*dy), 0))           AS total_distance_m,
                   MAX(t) - MIN(t)                                 AS duration_sec,
                   MAX(CASE WHEN dt > 0 THEN SQRT(dx*dx + dy*dy) / dt END)
                                                                   AS max_speed_m_s,
                   MIN(x) AS min_x, MAX(x) AS max_x,
                   MIN(y) AS min_y, MAX(y) AS max_y,
                   MIN(x0) AS x0, MIN(y0) AS y0, MIN(yaw0) AS yaw0,
                   MIN(x1) AS x1, MIN(y1) AS y1, MIN(yaw1) AS yaw1
            FROM d
        """, limit=1)
        r = rows[0]
        duration = r["duration_sec"] or 0.0
        distance = r["total_distance_m"] or 0.0
        return {
            "frame": frame,
            "num_points": r["num_points"],
            "total_distance_m": distance,
            "duration_sec": duration,
            "avg_speed_m_s": distance / duration if duration > 0 else 0.0,
            "max_speed_m_s": r["max_speed_m_s"] or 0.0,
            "start_pose": {"x": r["x0"], "y": r["y0"], "yaw": r["yaw0"]},
            "end_pose": {"x": r["x1"], "y": r["y1"], "yaw": r["yaw1"]},
            "bounding_box": {"min_x": r["min_x"], "max_x": r["max_x"],
                             "min_y": r["min_y"], "max_y": r["max_y"]},
        }

    limit = max(1, limit)
    rows = _query(campaign_id, _stride_sql(source, "t", limit), limit=limit)
    total = rows[0]["_n"] if rows else 0
    return {
        "frame": frame,
        "total_points": total,
        "returned_points": len(rows),
        "sampled": total > len(rows),
        "points": [{"timestamp": r["t"], "x": r["x"], "y": r["y"], "yaw": r["yaw"]}
                   for r in rows],
    }


@_reporting
def nav_get_action_feedback(
    campaign_id: str,
    config_name: str,
    run_id: int,
    limit: int = 200,
) -> dict:
    """Get navigation action feedback for a run.

    Queried from the table the ``rosbags_action_to_csv`` postprocessing step produced for
    the ``navigate_to_pose`` action, so it answers on either backend. The columns are
    whatever that recording holds; they are reported in ``columns``.

    Args:
        campaign_id: Campaign identifier.
        config_name: Configuration directory name.
        run_id: Run index within that configuration.
        limit: Maximum rows to return; the result is sampled at an even stride when there
            are more. ``total_rows`` always states the true count.

    Returns:
        ``{table, columns, total_rows, returned_rows, sampled, rows}`` or ``{error}``.
    """
    table = _find_table(campaign_id,
                        ("*navigate_to_pose*feedback*", "*nav*feedback*"),
                        "rosbags_action_to_csv")
    columns = [c for c in _tables(campaign_id)[table] if not c.startswith("_")]
    limit = max(1, limit)
    quoted = ", ".join(f'"{c}"' for c in columns)
    order = '"timestamp"' if "timestamp" in columns else "rowid"
    rows = _query(campaign_id, _stride_sql(
        f'SELECT {quoted}, rowid FROM "{table}" '
        f'WHERE config_name = {_lit(config_name)} '
        f'AND CAST(run_id AS INTEGER) = {int(run_id)}',
        order, limit), limit=limit)
    total = rows[0]["_n"] if rows else 0
    return {
        "table": table,
        "columns": columns,
        "total_rows": total,
        "returned_rows": len(rows),
        "sampled": total > len(rows),
        "rows": [{c: r[c] for c in columns} for r in rows],
    }


def _planned_path(campaign_id: str, config_name: str) -> list[dict]:
    """The configuration's planned path waypoints, from ``_transient/configurations.yaml``.

    Not from SQL, and this was checked rather than assumed: ``_path`` is an
    ``other_values`` entry written by the nav path variations
    (``robovast_nav/variation/path_variation.py``), so it never reaches
    ``unit.params_json`` (which holds only the varied parameters) nor ``config_view``
    (the ``.vast`` as authored). ``configurations.yaml`` is the only record of it.
    """
    configurations = _read_yaml(campaign_id, "_transient", "configurations.yaml")
    for cfg in configurations.get("configs", []) or []:
        if cfg.get("name") == config_name:
            path = cfg.get("_path")
            if not path:
                raise NavDataError(
                    f"config {config_name!r} records no planned path (`_path`), so it "
                    "was not generated by a nav path variation — there is nothing to "
                    "compare the trajectory against.")
            return path
    raise NavDataError(
        f"config {config_name!r} is not in this campaign's configurations.yaml.")


@_reporting
def nav_get_path_deviation(
    campaign_id: str,
    config_name: str,
    run_id: int,
    frame: str = "base_link",
    limit: int = _QUERY_MAX_ROWS,
) -> dict:
    """How closely did it follow the plan? Cross-track error and path efficiency.

    Cross-track error is each pose's distance to the nearest planned segment. Needs a
    configuration generated by a nav path variation (only those record a planned path).

    Args:
        campaign_id: Campaign identifier.
        config_name: Configuration directory name.
        run_id: Run index within that configuration.
        frame: TF frame (default ``base_link``).
        limit: Maximum poses for the cross-track pass.

    Returns:
        ``{mean_cross_track_error_m, max_cross_track_error_m, actual_distance_m,
        planned_distance_m, efficiency_ratio, points_used, total_points, sampled}``
        or ``{error}``.

        ``actual_distance_m`` is summed over every recorded pose and does not change with
        ``limit``; the cross-track figures use the poses fetched, which
        ``points_used``/``total_points`` report.
    """
    _require_poses(campaign_id, config_name, run_id, frame)
    planned = _planned_path(campaign_id, config_name)

    source = _pose_source(campaign_id, config_name, run_id, frame)
    limit = max(2, min(limit, _QUERY_MAX_ROWS))
    rows = _query(campaign_id, _stride_sql(source, "t", limit), limit=limit)
    if not rows:
        raise NavDataError(
            f"no poses in frame {frame!r} for run {run_id} of {config_name!r}.")
    total_points = rows[0]["_n"]

    # Summed in the database over the full recording, so thinning the cross-track pass
    # cannot quietly shorten the distance the robot is reported to have driven.
    actual_dist = _query(campaign_id, f"""
        WITH p AS ({source}),
             d AS (SELECT x - LAG(x) OVER w AS dx, y - LAG(y) OVER w AS dy
                   FROM p WINDOW w AS (ORDER BY t))
        SELECT SUM(COALESCE(SQRT(dx*dx + dy*dy), 0)) AS m FROM d
    """, limit=1)[0]["m"] or 0.0

    planned_dist = sum(
        math.sqrt((planned[i]["x"] - planned[i - 1]["x"]) ** 2 +
                  (planned[i]["y"] - planned[i - 1]["y"]) ** 2)
        for i in range(1, len(planned)))

    cross_track = []
    for r in rows:
        nearest = float("inf")
        for i in range(len(planned) - 1):
            nearest = min(nearest, _point_to_segment_distance(
                r["x"], r["y"],
                planned[i]["x"], planned[i]["y"],
                planned[i + 1]["x"], planned[i + 1]["y"]))
        cross_track.append(nearest)

    return {
        "mean_cross_track_error_m": sum(cross_track) / len(cross_track),
        "max_cross_track_error_m": max(cross_track),
        "actual_distance_m": actual_dist,
        "planned_distance_m": planned_dist,
        "efficiency_ratio": planned_dist / actual_dist if actual_dist > 0 else None,
        "points_used": len(rows),
        "total_points": total_points,
        "sampled": total_points > len(rows),
    }


def _map_dir_and_yaml(campaign_id: str, config_name: str) -> tuple[tuple, str]:
    """``((path, parts…), yaml_name)`` for the configuration's map.

    Preferred source is the configuration's own resolved ``map_file`` scenario parameter,
    which is campaign-relative; the ``_config/maps/`` listing is used when the parameter
    is absent (a scenario that hard-codes its map).
    """
    map_file = _scenario_params(campaign_id, config_name, required=False).get("map_file")
    if map_file:
        parts = str(map_file).strip("/").split("/")
        return tuple(parts[:-1]), parts[-1]

    prefix = (config_name, "_config", "maps")
    try:
        yamls = [e for e in _list_dir(campaign_id, *prefix) if e.endswith(".yaml")]
    except NavDataError:
        yamls = []  # no maps/ directory at all — the same verdict as an empty one
    if not yamls:
        raise NavDataError(
            f"config {config_name!r} has no 'map_file' scenario parameter and no "
            f"*.yaml under _config/maps/ — this is not a navigation configuration.")
    return prefix, yamls[0]


@_reporting
def nav_get_map_info(campaign_id: str, config_name: str,
                     occupancy: bool = False) -> dict:
    """Get a navigation configuration's map: metadata, and optionally cell occupancy.

    Args:
        campaign_id: Campaign identifier.
        config_name: Configuration directory name.
        occupancy: Also read the image and count occupied / free / unknown cells against
            the YAML's thresholds. Costs a transfer of the raster; the metadata alone
            does not.

    Returns:
        ``{map_name, resolution, origin, occupied_thresh, free_thresh, negate,
        image_file, width_px, height_px, width_m, height_m}``, plus with ``occupancy``
        ``{total_cells, occupied_cells, free_cells, unknown_cells, occupied_ratio,
        free_ratio, unknown_ratio}``. Or ``{error}``.
    """
    prefix, yaml_name = _map_dir_and_yaml(campaign_id, config_name)
    map_config = _read_yaml(campaign_id, *prefix, yaml_name)

    result: dict[str, Any] = {
        "map_name": yaml_name.rsplit(".", 1)[0],
        "resolution": map_config.get("resolution"),
        "origin": map_config.get("origin"),
        "occupied_thresh": map_config.get("occupied_thresh"),
        "free_thresh": map_config.get("free_thresh"),
        "negate": map_config.get("negate"),
        "image_file": map_config.get("image"),
    }

    image_name = map_config.get("image", "")
    if not image_name:
        if occupancy:
            raise NavDataError(
                f"map {yaml_name!r} declares no 'image', so there are no cells to count.")
        return result

    try:
        from PIL import Image as PILImage  # pylint: disable=import-outside-toplevel
    except ImportError as e:
        if occupancy:
            raise NavDataError("Pillow is not installed; cannot read the map raster.") from e
        return result

    with _materialized(campaign_id, prefix, [image_name]) as root:
        with PILImage.open(root / image_name) as img:
            width, height = img.size
            arr = np.array(img, dtype=float) / 255.0 if occupancy else None

    result["width_px"] = width
    result["height_px"] = height
    resolution = map_config.get("resolution") or 0.05
    result["width_m"] = width * resolution
    result["height_m"] = height * resolution

    if occupancy:
        occupied_thresh = map_config.get("occupied_thresh", 0.65)
        free_thresh = map_config.get("free_thresh", 0.196)
        total = arr.size
        occupied = int(np.sum(arr < free_thresh))
        free_cells = int(np.sum(arr > occupied_thresh))
        unknown = total - occupied - free_cells
        result.update({
            "total_cells": total,
            "occupied_cells": occupied,
            "free_cells": free_cells,
            "unknown_cells": unknown,
            "occupied_ratio": occupied / total,
            "free_ratio": free_cells / total,
            "unknown_ratio": unknown / total,
        })
    return result


def draw_map(
    campaign_id: str,
    config_name: str,
    layers: list[dict] | None = None,
    figsize: list[int] | None = None,
    title: str | None = None,
    show_legend: bool = True,
) -> Image:
    """Render the configuration's map with overlays — a trajectory, goals, obstacles.

    All coordinates are world metres. Feed it points from ``nav_get_trajectory`` or
    ``nav_get_obstacles``.

    Args:
        campaign_id: Campaign identifier.
        config_name: Configuration directory name.
        layers: Drawn in order. Every layer takes ``color``, ``alpha``, ``label``
            (matplotlib names), plus by ``type``:
            ``path`` — ``points`` [[x,y],…], ``linewidth``, ``show_endpoints``;
            ``points`` — ``points``, ``marker`` (matplotlib code), ``size``;
            ``circle`` — ``x``, ``y``, ``radius``;
            ``rectangle`` — ``x``, ``y``, ``width``, ``height``, ``yaw``;
            ``polygon`` — ``points`` (≥3);
            ``arrow`` — ``x``, ``y``, ``dx``, ``dy``, ``head_width``.
        figsize: ``[width, height]`` in inches (default ``[12, 10]``).
        title: Title drawn above the map.
        show_legend: Draw a legend when any layer has a ``label``.

    Raises:
        NavDataError: The configuration has no map (an image tool has no result dict to
            carry an ``{"error": …}`` in, so it raises instead).
    """
    from robovast_nav.gui.map_visualizer import MapVisualizer  # pylint: disable=import-outside-toplevel

    # Straight from the configuration's own resolved scenario parameters, rather than the
    # campaign metadata.yaml this used to read: that file is written by postprocessing, so
    # drawing the map of a campaign that had merely run used to fail.
    prefix, yaml_name = _map_dir_and_yaml(campaign_id, config_name)
    map_config = _read_yaml(campaign_id, *prefix, yaml_name)
    image_name = map_config.get("image")
    if not image_name:
        raise NavDataError(f"map {yaml_name!r} declares no 'image' to draw.")

    # The visualizer opens a path, and a cluster campaign's map is an object store entry.
    # Both files land in one temp dir under their own names so the YAML's relative
    # ``image:`` reference still resolves.
    with _materialized(campaign_id, prefix, [yaml_name, image_name]) as root:
        viz = MapVisualizer()
        if not viz.load_map(str(root / yaml_name)):
            raise NavDataError(f"could not load the map of config {config_name!r}")
        fw, fh = (figsize[0], figsize[1]) if figsize and len(figsize) == 2 else (12, 10)
        fig, ax = viz.create_figure(figsize=(fw, fh))

    if title:
        ax.set_title(title)

    for layer in layers or []:
        ltype = layer.get("type", "").lower()
        color = layer.get("color", "red")
        alpha = layer.get("alpha", 0.8)
        label = layer.get("label", None)

        if ltype == "path":
            raw = layer.get("points", [])
            if len(raw) >= 2:
                viz.draw_path(
                    [(p[0], p[1]) for p in raw],
                    color=color,
                    linewidth=layer.get("linewidth", 2.0),
                    alpha=alpha,
                    label=label or "Path",
                    show_endpoints=layer.get("show_endpoints", True),
                )

        elif ltype == "points":
            raw = layer.get("points", [])
            if raw:
                ax.plot(
                    [p[0] for p in raw],
                    [p[1] for p in raw],
                    color=color,
                    marker=layer.get("marker", "o"),
                    markersize=layer.get("size", 8),
                    alpha=alpha,
                    label=label or "Points",
                    linestyle="None",
                )

        elif ltype == "circle":
            ax.add_patch(plt.Circle(
                (layer["x"], layer["y"]),
                layer.get("radius", 0.5),
                color=color, alpha=alpha, label=label,
            ))

        elif ltype == "rectangle":
            x, y = layer["x"], layer["y"]
            w, h = layer.get("width", 1.0), layer.get("height", 1.0)
            yaw = layer.get("yaw", 0.0)
            corners = np.array([
                [-w / 2, -h / 2],
                [w / 2, -h / 2],
                [w / 2, h / 2],
                [-w / 2, h / 2],
            ])
            if yaw != 0.0:
                c, s = np.cos(yaw), np.sin(yaw)
                corners = corners @ np.array([[c, -s], [s, c]]).T
            corners[:, 0] += x
            corners[:, 1] += y
            ax.add_patch(mpatches.Polygon(
                corners, closed=True,
                color=color, alpha=alpha, label=label,
            ))

        elif ltype == "polygon":
            raw = layer.get("points", [])
            if len(raw) >= 3:
                ax.add_patch(mpatches.Polygon(
                    np.array([[p[0], p[1]] for p in raw]),
                    closed=True,
                    color=color, alpha=alpha, label=label,
                ))

        elif ltype == "arrow":
            head_w = layer.get("head_width", 0.1)
            ax.arrow(
                layer["x"], layer["y"],
                layer.get("dx", 0.0), layer.get("dy", 0.0),
                head_width=head_w, head_length=head_w,
                fc=color, ec=color, alpha=alpha,
                label=label,
                length_includes_head=True,
            )

    if show_legend:
        handles, labels_list = ax.get_legend_handles_labels()
        if handles:
            ax.legend(handles, labels_list)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return Image(data=buf.getvalue(), format="png")


# ---------------------------------------------------------------------------
# Plugin class
# ---------------------------------------------------------------------------

_TOOLS = [
    nav_get_obstacles,
    nav_get_trajectory,
    nav_get_action_feedback,
    nav_get_path_deviation,
    nav_get_map_info,
    draw_map,
]


class NavMCPPlugin:
    """Registers navigation-related MCP tools.

    Provides introspection into the nav variation types, navigation
    analysis (trajectory, path deviation), and environment/map tools.
    """

    name = "nav"

    def register(self, mcp: FastMCP) -> None:
        for fn in _TOOLS:
            mcp.tool()(fn)
