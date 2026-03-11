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

"""MCP plugin for robovast-nav: navigation analysis, environment, and map tools."""

from __future__ import annotations

import csv
import io
import json
import logging
import math
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import matplotlib
import numpy as np
import yaml
from matplotlib import patches as mpatches
from mcp.server.fastmcp import FastMCP, Image

from robovast.common.campaign_data import (
    read_resolved_configurations,
    read_scenario_config,
)
from robovast.evaluation.mcp_server.plugin_common import _get_config_by_identifier_or_name
from robovast.evaluation.mcp_server.results_resolver import (
    resolve_campaign_path,
    resolve_config_path,
    resolve_run_path,
)

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402  # pylint: disable=wrong-import-position,ungrouped-imports

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _downsample_rows(rows: list, max_rows: int) -> list:
    """Return at most *max_rows* rows using stride-based sampling."""
    if len(rows) <= max_rows:
        return rows
    stride = len(rows) / max_rows
    return [rows[int(i * stride)] for i in range(max_rows)]


def _quaternion_to_yaw(x: float, y: float, z: float, w: float) -> float:
    """Convert quaternion to yaw angle in radians."""
    siny_cosp = 2.0 * (w * z + x * y)
    cosy_cosp = 1.0 - 2.0 * (y * y + z * z)
    return math.atan2(siny_cosp, cosy_cosp)


def _find_jsonld_file(campaign: str, filename: str) -> Path | None:
    """Find a JSON-LD file in the first environment under _transient/."""
    campaign_path = resolve_campaign_path(campaign)
    transient = campaign_path / "_transient"
    if not transient.is_dir():
        return None
    for d in sorted(transient.iterdir()):
        candidate = d / "json-ld" / filename
        if candidate.exists():
            return candidate
    return None


def _read_poses_csv(path: Path, frame: str) -> list[dict]:
    """Read poses.csv, filtering by frame name."""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        first_line = f.readline()
        if first_line.startswith("#"):
            reader = csv.DictReader(f)
        else:
            f.seek(0)
            reader = csv.DictReader(f)
        rows = [r for r in reader if r.get("frame") == frame]
    return rows


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


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def nav_describe_data_model() -> dict[str, str]:
    """Return descriptions of the core navigation data-model types.

    These types (``Position``, ``Orientation``, ``Pose``,
    ``StaticObject``) are the building blocks used by all nav
    variation types.
    """
    return {
        "Position": "2D position with x (float) and y (float) coordinates in metres.",
        "Orientation": "Heading in the 2D plane expressed as yaw (float) in radians.",
        "Pose": "Combined Position and Orientation representing a robot pose.",
        "Object": (
            "An obstacle/object with entity_name (str), model (str), "
            "spawn_pose (Pose), and optional xacro_arguments (str). It can be spawned within the simulation."
        ),
    }


def nav_get_planned_path(campaign: str, config: str) -> dict:
    """Get the planned navigation path waypoints for a config.

    Args:
        campaign: Campaign name.
        config: Configuration name.
    """
    campaign_path = resolve_campaign_path(campaign)
    try:
        configurations = read_resolved_configurations(campaign_path)
    except FileNotFoundError:
        return {"error": "no planned path found."}

    for cfg in configurations.get("configs", []):
        if cfg.get("name") == config:
            path = cfg.get("_path")
            if path:
                return {
                    "num_waypoints": len(path),
                    "path_length": cfg.get("_path_length"),
                    "waypoints": path,
                }
            return {"error": "Data source is available, but no path found. May not be a navigation config."}

    return {"error": f"Config '{config}' not found in configurations."}


def nav_get_obstacles(campaign: str, config: str) -> list[dict]:
    """Get static obstacle definitions for a navigation config.

    Returns a list of obstacles with entity_name, model, position,
    orientation, and xacro_arguments.

    Args:
        campaign: Campaign name.
        config: Configuration name.
    """
    config_path = resolve_config_path(campaign, config)
    params = read_scenario_config(config_path)
    objects = params.get("static_objects", [])
    if not objects:
        return []

    result = []
    for obj in objects:
        entry: dict[str, Any] = {"entity_name": obj.get("entity_name")}
        entry["model"] = obj.get("model")
        pose = obj.get("spawn_pose", {})
        pos = pose.get("position", {})
        orient = pose.get("orientation", {})
        entry["x"] = pos.get("x")
        entry["y"] = pos.get("y")
        entry["yaw"] = orient.get("yaw")
        if "xacro_arguments" in obj:
            entry["xacro_arguments"] = obj["xacro_arguments"]
        result.append(entry)
    return result


def nav_get_trajectory(
    campaign: str,
    config: str,
    run: int,
    frame: str = "base_link",
    max_points: int = 200,
) -> dict:
    """Get the robot trajectory for a run.

    Reads ``poses.csv`` (produced by ``rosbags_tf_to_csv``
    postprocessing) and returns downsampled trajectory points.

    Args:
        campaign: Campaign name.
        config: Configuration name.
        run: Run number.
        frame: TF frame name to extract (default ``base_link``).
        max_points: Maximum trajectory points (default 200).
    """
    run_path = resolve_run_path(campaign, config, run)
    csv_path = run_path / "poses.csv"
    if not csv_path.exists():
        return {
            "error": (
                "poses.csv not found. Run 'vast analysis postprocess' "
                "with rosbags_tf_to_csv configured in the .vast file."
            )
        }

    rows = _read_poses_csv(csv_path, frame)
    if not rows:
        return {"error": f"No data found for frame '{frame}' in poses.csv."}

    sampled = _downsample_rows(rows, max_points)
    points = []
    for r in sampled:
        yaw = _quaternion_to_yaw(
            float(r.get("orientation.x", 0)),
            float(r.get("orientation.y", 0)),
            float(r.get("orientation.z", 0)),
            float(r.get("orientation.w", 1)),
        )
        points.append({
            "timestamp": float(r.get("timestamp", 0)),
            "x": float(r.get("position.x", 0)),
            "y": float(r.get("position.y", 0)),
            "yaw": yaw,
        })

    return {
        "frame": frame,
        "total_points": len(rows),
        "returned_points": len(points),
        "points": points,
    }


def nav_get_trajectory_stats(
    campaign: str,
    config: str,
    run: int,
    frame: str = "base_link",
) -> dict:
    """Compute trajectory statistics for a run.

    Returns total distance, duration, average/max speed, start/end
    pose, and bounding box.

    Requires ``rosbags_tf_to_csv`` postprocessing.

    Args:
        campaign: Campaign name.
        config: Configuration name.
        run: Run number.
        frame: TF frame name (default ``base_link``).
    """
    run_path = resolve_run_path(campaign, config, run)
    csv_path = run_path / "poses.csv"
    if not csv_path.exists():
        return {
            "error": (
                "poses.csv not found. Run 'vast analysis postprocess' "
                "with rosbags_tf_to_csv configured in the .vast file."
            )
        }

    rows = _read_poses_csv(csv_path, frame)
    if not rows:
        return {"error": f"No data found for frame '{frame}' in poses.csv."}

    xs = [float(r.get("position.x", 0)) for r in rows]
    ys = [float(r.get("position.y", 0)) for r in rows]
    ts = [float(r.get("timestamp", 0)) for r in rows]

    total_dist = 0.0
    speeds = []
    for i in range(1, len(xs)):
        dx = xs[i] - xs[i - 1]
        dy = ys[i] - ys[i - 1]
        dt = ts[i] - ts[i - 1]
        dist = math.sqrt(dx * dx + dy * dy)
        total_dist += dist
        if dt > 0:
            speeds.append(dist / dt)

    duration = ts[-1] - ts[0] if len(ts) > 1 else 0.0
    avg_speed = total_dist / duration if duration > 0 else 0.0
    max_speed = max(speeds) if speeds else 0.0

    start_yaw = _quaternion_to_yaw(
        float(rows[0].get("orientation.x", 0)),
        float(rows[0].get("orientation.y", 0)),
        float(rows[0].get("orientation.z", 0)),
        float(rows[0].get("orientation.w", 1)),
    )
    end_yaw = _quaternion_to_yaw(
        float(rows[-1].get("orientation.x", 0)),
        float(rows[-1].get("orientation.y", 0)),
        float(rows[-1].get("orientation.z", 0)),
        float(rows[-1].get("orientation.w", 1)),
    )

    return {
        "frame": frame,
        "num_points": len(rows),
        "total_distance_m": total_dist,
        "duration_sec": duration,
        "avg_speed_m_s": avg_speed,
        "max_speed_m_s": max_speed,
        "start_pose": {"x": xs[0], "y": ys[0], "yaw": start_yaw},
        "end_pose": {"x": xs[-1], "y": ys[-1], "yaw": end_yaw},
        "bounding_box": {
            "min_x": min(xs), "max_x": max(xs),
            "min_y": min(ys), "max_y": max(ys),
        },
    }


def nav_get_action_feedback(
    campaign: str,
    config: str,
    run: int,
    max_rows: int = 200,
) -> dict:
    """Get navigation action feedback data for a run.

    Args:
        campaign: Campaign name.
        config: Configuration name.
        run: Run number.
        max_rows: Maximum rows to return (default 200).
    """
    run_path = resolve_run_path(campaign, config, run)

    candidates = (
        list(run_path.glob("*navigate_to_pose*feedback*.csv"))
        + list(run_path.glob("*nav*feedback*.csv"))
    )
    if not candidates:
        return {
            "error": (
                "Navigation action feedback CSV not found. Run 'vast analysis postprocess' "
                "with rosbags_action_to_csv configured (action: navigate_to_pose)."
            )
        }

    csv_path = candidates[0]
    with open(csv_path, "r", encoding="utf-8", errors="replace") as f:
        first_line = f.readline()
        if first_line.startswith("#"):
            reader = csv.DictReader(f)
        else:
            f.seek(0)
            reader = csv.DictReader(f)
        rows = list(reader)

    sampled = _downsample_rows(rows, max_rows)
    return {
        "source_file": csv_path.name,
        "total_rows": len(rows),
        "returned_rows": len(sampled),
        "columns": list(reader.fieldnames or []),
        "data": sampled,
    }


def nav_get_path_deviation(
    campaign: str,
    config: str,
    run: int,
    frame: str = "base_link",
) -> dict:
    """Compute path deviation between actual trajectory and planned path.

    Compares the actual trajectory from ``poses.csv`` against the
    planned path from ``configurations.yaml``.  Returns cross-track
    error statistics and efficiency ratio.

    Requires ``rosbags_tf_to_csv`` postprocessing.

    Args:
        campaign: Campaign name.
        config: Configuration name.
        run: Run number.
        frame: TF frame name (default ``base_link``).
    """
    run_path = resolve_run_path(campaign, config, run)
    csv_path = run_path / "poses.csv"
    if not csv_path.exists():
        return {
            "error": (
                "poses.csv not found. Run 'vast analysis postprocess' "
                "with rosbags_tf_to_csv configured in the .vast file."
            )
        }

    rows = _read_poses_csv(csv_path, frame)
    if not rows:
        return {"error": f"No data found for frame '{frame}' in poses.csv."}

    campaign_path = resolve_campaign_path(campaign)
    try:
        configurations = read_resolved_configurations(campaign_path)
    except FileNotFoundError:
        return {"error": "configurations.yaml not found."}

    planned_path = None
    for cfg in configurations.get("configs", []):
        if cfg.get("name") == config:
            planned_path = cfg.get("_path")
            break

    if not planned_path:
        return {"error": "No planned path found for this config."}

    actual_xs = [float(r.get("position.x", 0)) for r in rows]
    actual_ys = [float(r.get("position.y", 0)) for r in rows]
    actual_dist = sum(
        math.sqrt((actual_xs[i] - actual_xs[i - 1]) ** 2 + (actual_ys[i] - actual_ys[i - 1]) ** 2)
        for i in range(1, len(actual_xs))
    )

    planned_dist = sum(
        math.sqrt(
            (planned_path[i]["x"] - planned_path[i - 1]["x"]) ** 2 +
            (planned_path[i]["y"] - planned_path[i - 1]["y"]) ** 2
        )
        for i in range(1, len(planned_path))
    )

    cross_track_errors = []
    for ax, ay in zip(actual_xs, actual_ys):
        min_dist = float("inf")
        for i in range(len(planned_path) - 1):
            d = _point_to_segment_distance(
                ax, ay,
                planned_path[i]["x"], planned_path[i]["y"],
                planned_path[i + 1]["x"], planned_path[i + 1]["y"],
            )
            min_dist = min(min_dist, d)
        cross_track_errors.append(min_dist)

    mean_cte = sum(cross_track_errors) / len(cross_track_errors) if cross_track_errors else 0.0
    max_cte = max(cross_track_errors) if cross_track_errors else 0.0

    return {
        "mean_cross_track_error_m": mean_cte,
        "max_cross_track_error_m": max_cte,
        "actual_distance_m": actual_dist,
        "planned_distance_m": planned_dist,
        "efficiency_ratio": planned_dist / actual_dist if actual_dist > 0 else None,
    }


def nav_get_map_info(campaign: str, config: str) -> dict:
    """Get map metadata for a navigation configuration.

    Reads the map YAML file from ``_config/maps/`` and returns
    resolution, origin, dimensions, and threshold values.

    Args:
        campaign: Campaign name.
        config: Configuration name.
    """
    config_path = resolve_config_path(campaign, config)
    maps_dir = config_path / "_config" / "maps"
    if not maps_dir.is_dir():
        return {"error": "No maps/ directory found. This may not be a navigation campaign."}

    yaml_files = list(maps_dir.glob("*.yaml"))
    if not yaml_files:
        return {"error": "No map YAML file found in _config/maps/."}

    map_yaml_path = yaml_files[0]
    with open(map_yaml_path, "r", encoding="utf-8") as f:
        map_config = yaml.safe_load(f)

    result: dict[str, Any] = {
        "map_name": map_yaml_path.stem,
        "resolution": map_config.get("resolution"),
        "origin": map_config.get("origin"),
        "occupied_thresh": map_config.get("occupied_thresh"),
        "free_thresh": map_config.get("free_thresh"),
        "negate": map_config.get("negate"),
        "image_file": map_config.get("image"),
    }

    image_name = map_config.get("image", "")
    if image_name:
        image_path = maps_dir / image_name
        if image_path.exists():
            try:
                from PIL import Image as PILImage  # pylint: disable=import-outside-toplevel
                with PILImage.open(image_path) as img:
                    w, h = img.size
                    result["width_px"] = w
                    result["height_px"] = h
                    res = map_config.get("resolution", 0.05)
                    result["width_m"] = w * res
                    result["height_m"] = h * res
            except ImportError:
                pass

    return result

def nav_get_map_occupancy_stats(campaign: str, config: str) -> dict:
    """Compute occupancy statistics for a map.

    Reads the PGM image and computes occupied, free, and unknown
    cell counts based on the threshold values from the YAML file.

    Args:
        campaign: Campaign name.
        config: Configuration name.
    """
    config_path = resolve_config_path(campaign, config)
    maps_dir = config_path / "_config" / "maps"
    if not maps_dir.is_dir():
        return {"error": "No maps/ directory found."}

    yaml_files = list(maps_dir.glob("*.yaml"))
    if not yaml_files:
        return {"error": "No map YAML file found."}

    with open(yaml_files[0], "r", encoding="utf-8") as f:
        map_config = yaml.safe_load(f)

    image_name = map_config.get("image", "")
    image_path = maps_dir / image_name
    if not image_path.exists():
        return {"error": f"Map image not found: {image_name}"}

    try:
        from PIL import Image as PILImage  # pylint: disable=import-outside-toplevel
    except ImportError:
        return {"error": "PIL/numpy not available for map analysis."}

    with PILImage.open(image_path) as img:
        arr = np.array(img, dtype=float) / 255.0

    occupied_thresh = map_config.get("occupied_thresh", 0.65)
    free_thresh = map_config.get("free_thresh", 0.196)

    total = arr.size
    occupied = int(np.sum(arr < free_thresh))
    free_cells = int(np.sum(arr > occupied_thresh))
    unknown = total - occupied - free_cells

    return {
        "total_cells": total,
        "occupied_cells": occupied,
        "free_cells": free_cells,
        "unknown_cells": unknown,
        "occupied_ratio": occupied / total,
        "free_ratio": free_cells / total,
        "unknown_ratio": unknown / total,
    }


def draw_map(
    campaign: str,
    config: str,
    layers: list[dict] | None = None,
    figsize: list[int] | None = None,
    title: str | None = None,
    show_legend: bool = True,
) -> Image:
    """Render a map with overlaid layers and return a PNG image.

    All coordinates are world coordinates (metres).

    Args:
        campaign: Campaign name.
        config: Configuration name.
        layers: Ordered list of drawing layers. Each layer is a dict
            with a ``type`` key and type-specific fields. Common fields
            available on every layer: ``color`` (matplotlib colour
            string), ``alpha`` (opacity 0–1), ``label`` (legend label).

            **Layer types:**

            ``"path"`` — polyline through world-coordinate points.

            - ``points``: list of [x, y] pairs (required, ≥ 2).
            - ``linewidth``: line width in points (default 2.0).
            - ``show_endpoints``: draw start/end dot markers (default true).

            ``"points"`` — scatter markers at world-coordinate positions.

            - ``points``: list of [x, y] pairs (required).
            - ``marker``: matplotlib marker — ``"o"`` circle, ``"s"``
              square, ``"^"`` triangle, ``"*"`` star, ``"D"`` diamond,
              ``"P"`` plus-filled, ``"X"`` x-filled, ``"p"`` pentagon,
              ``"h"`` hexagon (default ``"o"``).
            - ``size``: marker size in points (default 8).

            ``"circle"`` — filled circle.

            - ``x``, ``y``: centre in world coordinates (required).
            - ``radius``: radius in metres (default 0.5).

            ``"rectangle"`` — filled rectangle, optionally rotated.

            - ``x``, ``y``: centre in world coordinates (required).
            - ``width``, ``height``: dimensions in metres (required).
            - ``yaw``: rotation in radians (default 0).

            ``"polygon"`` — filled closed polygon.

            - ``points``: list of [x, y] pairs (required, ≥ 3).

            ``"arrow"`` — directional arrow.

            - ``x``, ``y``: tail position (required).
            - ``dx``, ``dy``: delta to head in metres (required).
            - ``head_width``: arrowhead width in metres (default 0.1).

        figsize: Figure size as [width, height] in inches (default [12, 10]).
        title: Optional title drawn above the map.
        show_legend: Render a legend when any layer has a label (default true).
    """
    from robovast_nav.gui.map_visualizer import MapVisualizer  # pylint: disable=import-outside-toplevel

    cfg = _get_config_by_identifier_or_name(campaign, config)
    if cfg is None:
        raise FileNotFoundError(f"Config '{config}' not found in campaign '{campaign}'")
    map_file = cfg.get("config", {}).get("map_file")
    if not map_file:
        raise FileNotFoundError(f"No map_file in metadata for config '{config}'")
    map_yaml = str(resolve_campaign_path(campaign) / map_file)

    viz = MapVisualizer()
    if not viz.load_map(map_yaml):
        raise ValueError(f"Failed to load map from: {map_yaml}")

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


def display_simulation_screenshot(
    campaign: str,
    config: str,
    run: int,
    simulation_time: float,
) -> Image:
    """Return a simulation camera screenshot at the given simulation time.

    Locates the single WebM camera recording for the specified run and seeks
    to the frame corresponding to *simulation_time* (seconds since the start of the simulation,
    as used by ROS). The video time offset is derived from the rosbag
    ``metadata.yaml`` starting_time so that the correct frame is selected even
    when the bag was recorded mid-session.

    Args:
        campaign: Campaign directory name (e.g. ``performance-2026-03-10-213012``).
        config:   Config directory name (e.g. ``uniraster-59-1``).
        run:      Run index (integer, e.g. ``0``).
        simulation_time: Simulation time in seconds (float, since the start of the simulation).


    Returns:
        PNG screenshot as an MCP ``Image``.

    Raises:
        FileNotFoundError: No ``.webm`` file found in the run directory.
        ValueError: More than one ``.webm`` file exists (pass the correct one
            explicitly or clean up the directory).
    """
    import cv2  # pylint: disable=import-outside-toplevel

    run_path: Path = resolve_run_path(campaign, config, run)

    # --- Locate the single .webm file ---
    webm_files = list(run_path.glob("*.webm"))
    if len(webm_files) == 0:
        raise FileNotFoundError(f"No .webm file found in {run_path}")
    if len(webm_files) > 1:
        names = ", ".join(f.name for f in sorted(webm_files))
        raise ValueError(
            f"Multiple .webm files found in {run_path}: {names}. "
            "Cannot auto-select; please remove the unwanted file."
        )
    webm_path = webm_files[0]

    # --- Determine video seek offset from rosbag metadata ---
    seek_s = 0.0
    rosbag_dirs = [
        d for d in run_path.iterdir()
        if d.is_dir() and (d / "metadata.yaml").exists()
    ]
    if rosbag_dirs:
        meta_file = rosbag_dirs[0] / "metadata.yaml"
        try:
            with meta_file.open() as fh:
                meta = yaml.safe_load(fh)
            ns = (
                meta["rosbag2_bagfile_information"]["starting_time"][
                    "nanoseconds_since_epoch"
                ]
            )
            bag_start_s = ns / 1e9
            seek_s = max(0.0, simulation_time - bag_start_s)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not read rosbag metadata for time offset: %s", exc)
            seek_s = 0.0
    else:
        logger.warning(
            "No rosbag metadata.yaml found in %s; seeking to t=0", run_path
        )

    # --- Extract frame with OpenCV ---
    cap = cv2.VideoCapture(str(webm_path))
    try:
        # Determine video duration so we can clamp the seek position rather than
        # silently wrapping to the first frame when the timestamp overshoots the end.
        total_frames = cap.get(cv2.CAP_PROP_FRAME_COUNT)
        vid_fps = cap.get(cv2.CAP_PROP_FPS) or 1.0
        video_duration_s = (total_frames / vid_fps) if total_frames > 0 else None

        if video_duration_s is not None and seek_s >= video_duration_s:
            clamped = max(0.0, video_duration_s - 1.0 / vid_fps)
            logger.warning(
                "simulation_time %.3f s maps to seek offset %.3f s which exceeds "
                "video duration %.3f s for %s; clamping to last frame (%.3f s)",
                simulation_time,
                seek_s,
                video_duration_s,
                webm_path.name,
                clamped,
            )
            seek_s = clamped

        cap.set(cv2.CAP_PROP_POS_MSEC, seek_s * 1000.0)
        ret, frame = cap.read()
        if not ret:
            # Decoder-level seek failure (e.g. keyframe alignment) — last-resort fallback
            logger.warning(
                "Seek to %.3f s failed for %s; falling back to first frame",
                seek_s,
                webm_path.name,
            )
            cap.set(cv2.CAP_PROP_POS_MSEC, 0.0)
            ret, frame = cap.read()
        if not ret:
            raise RuntimeError(f"Could not read any frame from {webm_path}")
    finally:
        cap.release()

    # --- Encode frame as PNG and return ---
    success, png_buf = cv2.imencode(".png", frame)
    if not success:
        raise RuntimeError("cv2.imencode failed to produce PNG data")
    return Image(data=png_buf.tobytes(), format="png")


# ---------------------------------------------------------------------------
# Plugin class
# ---------------------------------------------------------------------------

_TOOLS = [
    nav_describe_data_model,
    # nav_get_path,
    nav_get_obstacles,
    nav_get_trajectory,
    nav_get_trajectory_stats,
    nav_get_action_feedback,
    nav_get_path_deviation,
    nav_get_map_info,
    nav_get_map_occupancy_stats,
    draw_map,
    display_simulation_screenshot,
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
