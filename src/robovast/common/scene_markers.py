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

"""What a variation contributes to the config view, as neutral geometry.

A variation type knows what it produced -- obstacles at poses, a planned path, a start and
a goal. The config view's panels know how to draw. This module is the vocabulary between
them, and it is deliberately about *shapes at places* rather than about navigation: a
marker names a box, a polyline or a pose, never "an obstacle" or "a goal". A panel can
therefore draw a variation it has never heard of, and a new variation needs no change in
any panel.

This replaces the desktop editor's ``GUI_CLASS`` / ``GUI_RENDERER_CLASS`` pair, where a
variation shipped a Qt widget and drew onto it imperatively (``draw_obstacle``,
``draw_path``). That coupled every variation to one toolkit and to one 2D projection; the
same contribution now feeds the 3D scene and the 2D map alike, and travels over HTTP.
"""

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: A point in world coordinates. ``z`` is optional because most callers work on the floor
#: plane and repeating a zero adds noise to every marker they build.
Point = list[float]


class SceneMarker(BaseModel):
    """One thing to draw, in world coordinates.

    ``kind`` selects which fields matter; a panel ignores a kind it cannot render rather
    than failing, so a package shipping a richer marker degrades instead of breaking an
    older UI.
    """

    model_config = ConfigDict(extra='forbid')

    kind: Literal['box', 'cylinder', 'sphere', 'pose', 'path', 'point']
    #: Where it is. Unused by ``path``, which carries ``points`` instead.
    pos: Optional[Point] = None
    #: ``box``: full extents ``[x, y, z]``. Ignored by the other kinds.
    size: Optional[Point] = None
    #: ``cylinder``/``sphere``.
    radius: Optional[float] = None
    #: ``cylinder``.
    height: Optional[float] = None
    #: Rotation about z, radians. ``box``/``pose``.
    yaw: Optional[float] = None
    #: ``path``: the polyline, in order.
    points: Optional[list[Point]] = None
    #: Shown on or beside the marker.
    label: str = ""
    #: CSS colour. Omitted lets the panel choose, which is what keeps a variation from
    #: having to know the view's palette.
    color: str = ""
    #: Markers sharing a group are shown and hidden together, and the group name is what a
    #: panel puts in its legend. Defaults to the contributing variation's type name.
    group: str = ""

    @model_validator(mode='after')
    def _kind_has_its_geometry(self):
        # A marker with no geometry draws nothing, and silently: it is exactly the case
        # where a variation "contributed a preview" and the view stayed empty.
        if self.kind == 'path':
            if not self.points:
                raise ValueError("a 'path' marker needs 'points'")
        elif self.pos is None:
            raise ValueError(f"a '{self.kind}' marker needs 'pos'")
        if self.kind == 'box' and not self.size:
            raise ValueError("a 'box' marker needs 'size' (full extents [x, y, z])")
        if self.kind in ('cylinder', 'sphere') and self.radius is None:
            raise ValueError(f"a '{self.kind}' marker needs 'radius'")
        return self


class ConfigViewContribution(BaseModel):
    """Everything one variation contributes for one resolved configuration.

    Both fields are optional and a variation contributing neither is the normal case --
    only a variation that produces *placement* has anything to draw.
    """

    model_config = ConfigDict(extra='forbid')

    markers: list[SceneMarker] = Field(default_factory=list)
    #: Named workspace-relative paths a panel may need to fetch, e.g.
    #: ``{"map": "environments/office/map.yaml"}`` for the 2D map panel. Named rather than
    #: positional so a panel asks for the role it wants and a variation that has no map is
    #: simply missing the key.
    files: dict[str, str] = Field(default_factory=dict)

    def merged_with(self, other: "ConfigViewContribution") -> "ConfigViewContribution":
        """This contribution plus *other*'s. Later files win on a key collision."""
        return ConfigViewContribution(
            markers=[*self.markers, *other.markers],
            files={**self.files, **other.files},
        )


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
