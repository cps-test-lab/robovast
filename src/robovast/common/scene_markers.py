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

"""Collecting what each variation contributes to the config view.

The **vocabulary** -- :class:`SceneMarker`, :class:`ConfigViewContribution` -- is defined in
:mod:`robovast.client.scene_markers`, because it is also the shape the service serves and the
web UI's types are generated from that schema. It is re-exported here so a variation author
imports one module and never has to know which distribution the model lives in.

What is *here* is the part that needs the variation classes: asking each of them, in order,
what it contributes for one resolved configuration.
"""

from typing import Any, Optional

from robovast.client.scene_markers import ConfigViewContribution, Point, SceneMarker

__all__ = ["ConfigViewContribution", "Point", "SceneMarker", "collect_contributions",
           "read_pose", "read_xy"]


def _field(value, name):
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


def read_xy(value) -> Optional[Point]:
    """``[x, y]`` -- or ``[x, y, z]`` when z is stated -- from a Position-ish mapping or object,
    else ``None``."""
    if value is None:
        return None
    x, y, z = _field(value, "x"), _field(value, "y"), _field(value, "z")
    if x is None or y is None:
        return None
    return [float(x), float(y)] if z is None else [float(x), float(y), float(z)]


def read_pose(value) -> tuple[Optional[Point], Optional[float]]:
    """``(pos, yaw)`` from a Pose-ish mapping or object: ``{position: {x, y}, orientation: {yaw}}``,
    or a bare ``{x, y}`` -- what a hand-written ``.vast`` pose looks like, so it is accepted rather
    than silently read as nothing. Either half may be absent."""
    if value is None:
        return None, None
    position, orientation = _field(value, "position"), _field(value, "orientation")
    yaw = _field(orientation, "yaw") if orientation is not None else None
    pos = read_xy(position) if position is not None else read_xy(value)
    return pos, (float(yaw) if yaw is not None else None)


def collect_contributions(config: dict, variation_classes, base_path: str) -> dict[str, Any]:
    """Ask every variation of one resolved *config* what it contributes.

    Returns the transport shape ``{markers, files, errors}``. A variation whose hook raises
    is reported in ``errors`` rather than dropped: a view that silently loses one
    variation's markers looks like a variation that placed nothing, which is the failure
    this repo's fail-loudly rule exists to prevent. The other variations still draw.
    """
    total = ConfigViewContribution()
    errors: list[str] = []
    for variation_class in variation_classes:
        name = getattr(variation_class, "__name__", str(variation_class))
        try:
            contributed = variation_class.config_view_data(config, base_path)
        except Exception as exc:  # noqa: BLE001 - one broken hook must not blank the view
            errors.append(f"{name}: {exc}")
            continue
        if contributed is None:
            continue
        # Default the group to the variation that produced it, so a view with two
        # populations can tell them apart without every plugin remembering to set it.
        for marker in contributed.markers:
            if not marker.group:
                marker.group = name
        total = total.merged_with(contributed)
    return {"markers": [m.model_dump(exclude_none=True) for m in total.markers],
            "files": total.files, "errors": errors}
