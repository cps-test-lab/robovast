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

"""A configuration drawn as a picture: its contributed markers, a run's track, a background.

The inputs are the ones the config view draws -- a configuration's contribution
(:func:`robovast.common.scene_markers.campaign_contribution`) -- so the picture an agent gets
and the panel a person sees cannot disagree about what was placed where. Nothing here knows
a domain: markers are the neutral :class:`~robovast.common.scene_markers.SceneMarker` shapes,
and a contributed file (an occupancy map, say) is drawn only by the package that knows its
format, through the config panel type that declares the file's role (see :func:`backgrounds`).

A side projection (``xz``, ``yz``) exists because not every robot works on a floor: an arm's
end effector or a drone is read from the side. A marker's ``pos`` is its footprint centre on
the floor, as in the 3D scene, so a box or cylinder rises from its ``z`` by its height.
"""

from __future__ import annotations

import io
import math
from typing import Callable, Optional

#: The world axes each projection draws, as indices into ``[x, y, z]``.
PROJECTIONS = {"xy": (0, 1), "xz": (0, 2), "yz": (1, 2)}

_TRACK_COLOR = "#d62728"
_MARKER_COLOR = "#1f77b4"


def backgrounds() -> tuple:
    """``({file role: panel type}, {entry point: why})`` over the installed config panel types.

    A panel type opts in by declaring ``FILE_ROLE`` and a ``plot(ax, path, projection, read)``
    beside its ``WEB_PANEL``: the package that renders a file format in the browser is the one
    that knows how to render it here.
    """
    from importlib.metadata import entry_points  # pylint: disable=import-outside-toplevel

    found, failed = {}, {}
    for entry in entry_points(group="robovast.panel_types"):
        try:
            panel = entry.load()
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            # "nothing installed draws this" and "what draws it did not load" are different
            # facts, and the second reads as the first on a picture that just leaves the
            # background out.
            failed[entry.name] = str(exc)
            continue
        role = getattr(panel, "FILE_ROLE", None)
        if role and callable(getattr(panel, "plot", None)):
            found[role] = panel
    return found, failed


def _point(value, dims) -> Optional[tuple]:
    """A marker coordinate on the projection's two axes; a missing ``z`` is the floor."""
    if not value:
        return None
    full = [float(v) for v in value] + [0.0] * (3 - len(value))
    return full[dims[0]], full[dims[1]]


def _draw_marker(ax, marker: dict, dims, patches) -> None:
    kind = marker.get("kind")
    color = marker.get("color") or _MARKER_COLOR
    label = marker.get("label") or None
    if kind == "path":
        pts = [_point(p, dims) for p in marker.get("points") or []]
        ax.plot([p[0] for p in pts], [p[1] for p in pts], color=color, linewidth=1.5,
                label=label)
        return
    at = _point(marker.get("pos"), dims)
    if at is None:
        return
    if kind in ("pose", "point"):
        ax.plot(*at, marker="o", color=color, markersize=6, linestyle="None", label=label)
        yaw = marker.get("yaw")
        if kind == "pose" and yaw is not None and dims == (0, 1):
            ax.annotate("", xy=(at[0] + 0.4 * math.cos(yaw), at[1] + 0.4 * math.sin(yaw)),
                        xytext=at, arrowprops={"arrowstyle": "->", "color": color})
        return
    yaw = marker.get("yaw") or 0.0
    if kind == "box":
        sx, sy, sz = ([float(v) for v in marker.get("size") or [0, 0, 0]] + [0, 0, 0])[:3]
    else:  # cylinder, sphere
        r = float(marker.get("radius") or 0.0)
        sx = sy = 2 * r
        sz = float(marker.get("height") or 2 * r)
    if dims == (0, 1):
        if kind == "box":
            patch = patches.Rectangle((at[0] - sx / 2, at[1] - sy / 2), sx, sy,
                                      angle=math.degrees(yaw), rotation_point="center")
        else:
            patch = patches.Circle(at, sx / 2)
    else:
        # The extent of a z-rotated footprint along one horizontal axis, and its height.
        c, s = abs(math.cos(yaw)), abs(math.sin(yaw))
        width = (sx * c + sy * s) if dims[0] == 0 else (sx * s + sy * c)
        bottom = at[1] - (sz / 2 if kind == "sphere" else 0.0)
        patch = patches.Rectangle((at[0] - width / 2, bottom), width, sz)
    patch.set(facecolor=color, alpha=0.35, edgecolor=color, label=label)
    ax.add_patch(patch)


def draw(contribution: dict, read: Callable[[str], bytes], *, track=None,
         track_label: str = "", projection: str = "xy", title: str = "") -> tuple[bytes, list]:
    """Render *contribution* (and *track*, ``[(x, y, z), ...]``) as a PNG.

    *read* returns the bytes of a campaign file by its campaign-relative path; a background
    renderer reads what its format needs through it. Returns ``(png, notes)``, *notes* naming
    what could not be drawn -- a file role with no installed renderer, one that declined the
    projection -- which is also printed on the figure, since an image has nowhere else to
    say it.

    Raises ``ImportError`` naming the extra when matplotlib is not installed, and
    ``ValueError`` for an unknown projection.
    """
    if projection not in PROJECTIONS:
        raise ValueError(f"projection {projection!r} is not one of {sorted(PROJECTIONS)}")
    try:
        import matplotlib  # pylint: disable=import-outside-toplevel
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt  # pylint: disable=import-outside-toplevel
        from matplotlib import patches  # pylint: disable=import-outside-toplevel
    except ImportError as exc:
        raise ImportError("drawing a configuration needs matplotlib: install robovast with "
                          "the 'analysis' extra (pip install 'robovast[analysis]')") from exc
    dims = PROJECTIONS[projection]
    fig, ax = plt.subplots(figsize=(10, 8))
    notes = []
    renderers, unloadable = backgrounds()
    for name, why in sorted(unloadable.items()):
        notes.append(f"panel type {name!r} did not load, so what it draws is missing: {why}")
    for role, path in sorted((contribution.get("files") or {}).items()):
        panel = renderers.get(role)
        if panel is None:
            notes.append(f"file role {role!r} ({path}): no installed panel type draws it")
        elif not panel.plot(ax, path, projection, read):
            notes.append(f"file role {role!r} is not drawn in the {projection} projection")
    for marker in contribution.get("markers") or []:
        _draw_marker(ax, marker, dims, patches)
    if track:
        ax.plot([p[dims[0]] for p in track], [p[dims[1]] for p in track], color=_TRACK_COLOR,
                linewidth=1.2, label=track_label or "track")
        ax.plot(*[track[0][d] for d in dims], marker="s", color=_TRACK_COLOR)
        ax.plot(*[track[-1][d] for d in dims], marker="X", color=_TRACK_COLOR, markersize=9)
    for error in contribution.get("errors") or []:
        notes.append(f"contribution: {error}")
    ax.set_aspect("equal", adjustable="datalim")
    ax.set_xlabel(f"{projection[0]} [m]")
    ax.set_ylabel(f"{projection[1]} [m]")
    if title:
        ax.set_title(title)
    handles, labels = ax.get_legend_handles_labels()
    if handles:
        unique = dict(zip(labels, handles))
        ax.legend(unique.values(), unique.keys(), loc="best", fontsize=8)
    if notes:
        fig.text(0.01, 0.01, "\n".join(notes), fontsize=7, color="#b00000", va="bottom")
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue(), notes
