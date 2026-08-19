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

"""What the nav variations contribute to the config view.

The port of the desktop editor's ``*GuiRenderer`` classes, which drew a planned path, its
goal poses and the placed obstacles onto a shared matplotlib map widget. Those drew
*imperatively into Qt*; these return :mod:`robovast.common.scene_markers` geometry, so the
same contribution feeds the 3D scene, the 2D map panel, and anything added later.

Collected here rather than on each variation class because the nav types contribute
overlapping pieces of one picture -- a path variation writes the path and the goals, an
obstacle variation adds boxes along it -- and the reading of a resolved configuration
(which key holds the goals, how a pose is shaped) is the same for all of them.
"""

from typing import Optional

from robovast.common.scene_markers import ConfigViewContribution, SceneMarker

from .object_shapes import get_object_type_from_model_path, get_obstacle_dimensions

#: The palette. Fixed here rather than left to the panels because these markers are read
#: together -- a path and the obstacles on it -- and a per-panel choice would give the same
#: configuration different colors in the 3D scene and the 2D map.
PATH_COLOR = "#f87171"
START_COLOR = "#60a5fa"
GOAL_COLOR = "#4ade80"
OBSTACLE_COLOR = "#fbbf24"
RASTER_COLOR = "#94a3b8"

#: How tall to draw an obstacle whose campaign declared no ``size``. Only reached on the
#: spawner-only path (a Gazebo-style campaign binding ``objects`` but not ``instances``),
#: where the extents genuinely are not in the configuration.
_FALLBACK_BOX = [0.5, 0.5, 1.0]


def _xy(value) -> Optional[list]:
    """``[x, y]`` from a Position/Point-ish mapping or object, else ``None``."""
    if value is None:
        return None
    if isinstance(value, dict):
        x, y = value.get("x"), value.get("y")
    else:
        x, y = getattr(value, "x", None), getattr(value, "y", None)
    if x is None or y is None:
        return None
    return [float(x), float(y)]


def _pose(value) -> tuple[Optional[list], Optional[float]]:
    """``([x, y], yaw)`` from a Pose-ish mapping or object."""
    if value is None:
        return None, None
    position = value.get("position") if isinstance(value, dict) else getattr(value, "position", None)
    orientation = (value.get("orientation") if isinstance(value, dict)
                   else getattr(value, "orientation", None))
    yaw = None
    if orientation is not None:
        yaw = (orientation.get("yaw") if isinstance(orientation, dict)
               else getattr(orientation, "yaw", None))
    # A bare position (no `position:` wrapper) is what a hand-written `.vast` pose looks
    # like, so accept it rather than silently drawing nothing.
    return _xy(position) if position is not None else _xy(value), (
        float(yaw) if yaw is not None else None)


def _as_list(value) -> list:
    """*value* as a list: a single pose and a list of them read the same way."""
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def path_markers(config: dict) -> list[SceneMarker]:
    """The planned path, the start and goal poses, and any raster sample points.

    Reads the *resolved destination* the variation recorded (``_goal_parameter_name``)
    rather than guessing a name: the campaign chooses what its goal parameter is called,
    and the desktop renderer that guessed drew nothing the moment an author picked a third.
    """
    markers: list[SceneMarker] = []
    params = config.get("config") or {}

    points = [p for p in (_xy(p) for p in (config.get("_path") or [])) if p]
    if points:
        markers.append(SceneMarker(kind="path", points=points, color=PATH_COLOR,
                                   label="planned path"))

    start_pos, start_yaw = _pose(params.get("start_pose"))
    if start_pos:
        markers.append(SceneMarker(kind="pose", pos=start_pos, yaw=start_yaw,
                                   color=START_COLOR, label="start"))

    goals = _as_list(params.get(config.get("_goal_parameter_name")))
    for i, goal in enumerate(goals):
        pos, yaw = _pose(goal)
        if pos:
            markers.append(SceneMarker(
                kind="pose", pos=pos, yaw=yaw, color=GOAL_COLOR,
                label="goal" if len(goals) == 1 else f"goal {i + 1}"))

    # The candidate grid a rasterized path search sampled from. Drawn small and faint: it
    # is context for why the path went where it did, not a result.
    for point in (config.get("_raster_points") or []):
        pos = _xy({"x": point[0], "y": point[1]}) if isinstance(point, (list, tuple)) else _xy(point)
        if pos:
            markers.append(SceneMarker(kind="point", pos=pos, color=RASTER_COLOR))

    return markers


def obstacle_markers(config: dict) -> list[SceneMarker]:
    """One box (or cylinder) per placed obstacle.

    Extents come from what the campaign **declared** -- the ``sim`` channel's ``instances``,
    which carry ``size`` because a simulator compiling the placement needs it. The desktop
    renderer instead parsed them back out of the spawner's xacro argument string, which
    ``ObstacleVariation`` itself records as a mistake: anything it could not parse fell back
    to a default shape, silently, so the drawn obstacle and the compiled one disagreed.

    A campaign that only spawns at run time (``objects`` bound, ``instances`` not) has no
    declared extents anywhere, and only there is the argument string consulted.
    """
    markers: list[SceneMarker] = []
    params = config.get("config") or {}
    objects = params.get(config.get("_objects_parameter_name")) or []

    # The simulator's view of the same placement, keyed by the name both channels use.
    instances = {}
    for entry in _instances_of(config):
        if isinstance(entry, dict) and entry.get("name"):
            instances[entry["name"]] = entry

    for obstacle in objects:
        entry = obstacle if isinstance(obstacle, dict) else _as_dict(obstacle)
        pos, yaw = _pose(entry.get("spawn_pose"))
        if not pos:
            continue
        declared = instances.get(entry.get("entity_name")) or {}
        size = declared.get("size")
        shape = declared.get("shape") or get_object_type_from_model_path(entry.get("model") or "")
        if size:
            markers.append(SceneMarker(
                kind="box", pos=pos, size=[float(v) for v in size], yaw=yaw,
                color=OBSTACLE_COLOR, label=entry.get("entity_name") or ""))
            continue
        dims = get_obstacle_dimensions(entry.get("xacro_arguments") or "")
        if shape == "cylinder":
            markers.append(SceneMarker(
                kind="cylinder", pos=pos, radius=float(dims.get("radius", 0.25)),
                height=float(dims.get("height", _FALLBACK_BOX[2])), color=OBSTACLE_COLOR,
                label=entry.get("entity_name") or ""))
        else:
            markers.append(SceneMarker(
                kind="box", pos=pos, yaw=yaw, color=OBSTACLE_COLOR,
                size=[float(dims.get("width", _FALLBACK_BOX[0])),
                      float(dims.get("length", _FALLBACK_BOX[1])),
                      float(dims.get("height", _FALLBACK_BOX[2]))],
                label=entry.get("entity_name") or ""))
    return markers


def _instances_of(config: dict) -> list:
    """The ``instances`` list this configuration wrote to the ``sim`` channel, if any.

    The destination is the campaign's to choose (``sim: {instances: plugins.boxes.instances}``),
    so it is found by shape -- a list of mappings carrying ``pos`` -- rather than by a key
    name this module would otherwise have to know.
    """
    for value in (config.get("sim") or {}).values():
        if isinstance(value, list) and value and isinstance(value[0], dict) and "pos" in value[0]:
            return value
    return []


def _as_dict(obj) -> dict:
    """A dataclass-ish obstacle as a plain dict."""
    from robovast.common import \
        convert_dataclasses_to_dict  # pylint: disable=import-outside-toplevel
    converted = convert_dataclasses_to_dict([obj])[0]
    return converted if isinstance(converted, dict) else {}


def map_file(config: dict) -> dict[str, str]:
    """``{"map": <path>}`` when the configuration knows its occupancy map, else ``{}``.

    The scenario parameter wins over the private key: ``_map_file`` is only recorded when
    the campaign did *not* set ``map_file`` itself, and it is an absolute path on the host
    that composed -- which means nothing to a browser fetching through the workspace.
    """
    params = config.get("config") or {}
    declared = params.get("map_file")
    if isinstance(declared, str) and declared:
        return {"map": declared}
    recorded = config.get("_map_file")
    if isinstance(recorded, str) and recorded and not recorded.startswith("/"):
        return {"map": recorded}
    return {}


def path_contribution(config: dict) -> ConfigViewContribution:
    """A path variation's contribution: the path, its endpoints, and the map to draw on."""
    return ConfigViewContribution(markers=path_markers(config), files=map_file(config))


def obstacle_contribution(config: dict) -> ConfigViewContribution:
    """An obstacle variation's contribution: the placed obstacles."""
    return ConfigViewContribution(markers=obstacle_markers(config), files=map_file(config))


def trigger_contribution(config: dict) -> ConfigViewContribution:
    """The obstacles, plus the point along the path whose arrival spawns them.

    The trigger travels as ``_spawn_trigger_point`` -- the resolved *value*, copied there by
    the variation because the campaign chooses what the slot is called, so nothing here has
    to know that name.
    """
    contribution = obstacle_contribution(config)
    pos = _xy(config.get("_spawn_trigger_point"))
    if pos:
# pydantic field; pylint sees the FieldInfo
        # pylint: disable-next=no-member
        contribution.markers.append(
            SceneMarker(kind="sphere", pos=pos, radius=0.15, color=OBSTACLE_COLOR,
                        label="spawn trigger"))
    return contribution
