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

"""What a ``.vast`` may say to a panel: one binding grammar, and the models panels declare.

A panel's *bindings* are the keys beside ``type``/``title``/``height`` on its declaration. They were
free-form extras interpreted by each panel, which means a misspelled one validated cleanly and drew
nothing -- the failure arrived in the browser as an empty panel, with nothing naming the key that was
ignored. A panel type declares a ``CONFIG_CLASS`` instead, exactly as a variation type does, and gets
validation, editor completion and ``get_plugin_details`` for free.

One grammar for every field, because a field's *source* is a separate question from its meaning: a map
is a map whether it is a checked-in path, a scenario parameter, or a file a variation generated.
"""

from typing import Any, ClassVar, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: Marker kinds a geometry panel can draw -- the neutral vocabulary of
#: :class:`robovast.client.scene_markers.SceneMarker`, mirrored here because a *declared* marker is
#: validated before any run exists and the client model is not importable from core.
MARKER_KINDS = ("box", "cylinder", "sphere", "pose", "path", "point")


class Binding(BaseModel):
    """Where a panel field's value comes from.

    Four sources, exactly one per binding:

    * a **literal** -- written directly (``map: files/depot.yaml``), which is the common case and so
      needs no wrapper: a bare scalar or list is read as one;
    * ``param:`` -- a resolved scenario parameter, so the field follows the selected configuration;
    * ``internal:`` -- an ``_``-prefixed key a variation left on the configuration (``_path``,
      ``_map_file``), named exactly as the configuration carries it;
    * ``role:`` -- a named entry of the configuration's contributed ``files`` (``map``).

    A source naming something a configuration does not have resolves to nothing, and the panel draws
    nothing for it. That is deliberate and matches the marker rule it generalises: a value silently
    defaulted is a wrong answer, an absent one is a visible question.
    """

    model_config = ConfigDict(extra='forbid')

    literal: Optional[Any] = None
    param: Optional[str] = None
    internal: Optional[str] = None
    role: Optional[str] = None

    #: The mapping keys that make a binding explicit. A mapping with none of them is itself a
    #: literal -- a pose is ``{x: 1, y: 2}``, and demanding ``literal:`` around it would be noise.
    _SOURCES: ClassVar[tuple] = ("literal", "param", "internal", "role")

    @model_validator(mode='before')
    @classmethod
    def _bare_value_is_a_literal(cls, v):
        if isinstance(v, dict) and any(k in v for k in cls._SOURCES):
            return v
        return {"literal": v}

    @model_validator(mode='after')
    def _exactly_one_source(self):
        given = [k for k in self._SOURCES if getattr(self, k) is not None]
        if len(given) != 1:
            raise ValueError(
                "a binding names exactly one source: a literal value, or one of "
                f"'param' / 'internal' / 'role'; got {given or 'nothing'}")
        return self


class DeclaredMarker(BaseModel):
    """A marker a ``.vast`` declares on a geometry panel, rather than a variation contributing it.

    The geometry fields are :class:`SceneMarker`'s; ``param``/``internal`` say where the position
    comes from when it is not written out, and ``offset`` is applied afterwards -- which is how a
    map-frame parameter is placed in a world-frame scene, declared in the file because nothing in a
    panel can know a campaign's frames.
    """

    model_config = ConfigDict(extra='forbid')

    kind: Literal[MARKER_KINDS]
    #: ``[x, y]`` or ``[x, y, z]``, when the position is written out.
    pos: Optional[list[float]] = None
    #: A resolved scenario parameter holding a pose, or a list of them (one marker each).
    param: Optional[str] = None
    #: An ``_``-prefixed key a variation left behind, holding a pose or -- for ``kind: path`` -- the
    #: polyline itself.
    internal: Optional[str] = None
    #: Added to the resolved position, component-wise.
    offset: Optional[list[float]] = None
    size: Optional[list[float]] = None
    radius: Optional[float] = None
    height: Optional[float] = None
    #: Rotation about z, radians.
    yaw: Optional[float] = None
    points: Optional[list[list[float]]] = None
    label: Optional[str] = None
    #: CSS colour; omit to let the panel choose.
    color: Optional[str] = None
    #: Markers sharing a group are shown and hidden together.
    group: Optional[str] = None

    @model_validator(mode='after')
    def _has_a_position_source(self):
        sources = [k for k in ("pos", "param", "internal", "points") if getattr(self, k) is not None]
        if not sources:
            raise ValueError(
                f"a declared {self.kind!r} marker needs a position: 'pos' (written out), 'param' / "
                "'internal' (read from the configuration), or 'points' for a path")
        return self


def _read_pose(value) -> tuple[Optional[list], Optional[float]]:
    """``([x, y] or [x, y, z], yaw)`` from a pose-shaped value: ``{position: {x, y}, orientation:
    {yaw}}``, or a bare ``{x, y}``, which is what a hand-written ``.vast`` pose often is."""
    if not isinstance(value, dict):
        return None, None
    position = value["position"] if isinstance(value.get("position"), dict) else value
    orientation = value.get("orientation")
    yaw = orientation.get("yaw") if isinstance(orientation, dict) else None
    yaw = float(yaw) if isinstance(yaw, (int, float)) else None
    x, y, z = position.get("x"), position.get("y"), position.get("z")
    if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
        return None, yaw
    pos = [float(x), float(y)] if not isinstance(z, (int, float)) else [float(x), float(y), float(z)]
    return pos, yaw


def _translate(pos: Optional[list], offset: Optional[list]) -> Optional[list]:
    if pos is None or not offset:
        return pos
    return [v + (offset[i] if i < len(offset) else 0.0) for i, v in enumerate(pos)]


def declared_markers(bindings: Optional[dict], config: dict) -> list[dict]:
    """The markers a panel's bindings declare, resolved against one configuration.

    The Python twin of the panel kit's ``declaredMarkers`` -- the web UI resolves the same
    declaration in the browser, and this is what a reader outside it (a video overlay, a report)
    uses, so ``param:`` and ``offset:`` mean one thing wherever a declared marker is drawn.

    *config* is a configuration as ``configurations.yaml`` records it: the scenario parameters
    under ``config`` and the ``_``-prefixed keys a variation left at the top level, which is the
    shape every Python reader of that file already consumes. Returns ``SceneMarker``-shaped
    mappings (``kind``, ``pos``, ``yaw``, ``points``, ``label``, ``color``, ``group``, ...).

    A ``param:`` naming something the configuration does not have yields no marker rather than a
    marker at the origin: a pose silently drawn at (0, 0) is a wrong answer, and an absent one is a
    visible question.
    """
    declared = (bindings or {}).get("markers")
    if not isinstance(declared, list):
        return []
    parameters = config.get("config") or {}
    out: list[dict] = []
    for entry in declared:
        if not isinstance(entry, dict):
            continue
        rest = {k: v for k, v in entry.items() if k not in ("param", "internal", "offset")}
        rest.setdefault("group", "declared")
        param, internal, offset = entry.get("param"), entry.get("internal"), entry.get("offset")
        if not param and not internal:
            pos = _translate(rest.get("pos"), offset)
            geometry = rest.get("points") if rest.get("kind") == "path" else pos
            if geometry:
                out.append({**rest, "pos": pos})
            continue
        # `param:` and `internal:` are the same question -- where does this position come from.
        # A path reads the value as its polyline; every other kind reads it as a pose, and a list
        # of them yields one numbered marker each.
        value = parameters.get(param) if param else config.get(internal)
        name = param or internal
        if rest.get("kind") == "path":
            points = [_translate(p, offset) for p in (_read_pose(p)[0] for p in (value or [])) if p]
            if points:
                out.append({**rest, "points": points, "label": rest.get("label") or name})
            continue
        values = value if isinstance(value, list) else [value]
        for i, one in enumerate(values):
            pos, yaw = _read_pose(one)
            moved = _translate(pos, offset)
            if moved is None:
                continue
            label = rest.get("label") or name
            out.append({
                **rest, "pos": moved,
                "yaw": rest["yaw"] if rest.get("yaw") is not None else yaw,
                "label": f"{label} {i + 1}" if len(values) > 1 else label,
            })
    return out


class Scene3DBindings(BaseModel):
    """``scene3d``: the campaign's world, with this configuration's placements on it."""

    model_config = ConfigDict(extra='forbid')

    markers: list[DeclaredMarker] = Field(
        default_factory=list,
        description=(
            "Geometry this campaign declares itself, drawn beside whatever its variations "
            "contributed. In the WORLD frame, the frame the compiled scene is in -- so a map-frame "
            "parameter needs an 'offset' to land correctly."))


class MarkersOnlyBindings(Scene3DBindings):
    """A geometry panel whose only binding is its markers. Named for what it is, so a panel type
    reads as declaring a contract rather than borrowing another panel's."""


#: Bindings for the core panel types, which are names rather than plugin classes and so have nowhere
#: to hang a ``CONFIG_CLASS``. Only the types that actually take bindings appear: ``parameters`` and
#: ``world`` read nothing from the file, and are absent rather than declared empty, because an empty
#: model would forbid the extras a future binding would arrive as.
BUILTIN_PANEL_BINDINGS = {
    "scene3d": Scene3DBindings,
}
