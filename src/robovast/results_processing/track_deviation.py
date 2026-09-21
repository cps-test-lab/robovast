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

"""How far one recorded track stayed from a path its configuration contributed.

The two halves live in different places. The track is rows of a pose-contract table in the
index; the path is a ``path`` marker a variation contributed for the configuration
(:func:`robovast.common.scene_markers.campaign_contribution`), which no table holds. The
track is read in full on a connection scoped to its campaign -- no reply cap applies to a
cursor, so nothing is thinned or cut off -- and the distance of every pose to the nearest
point of the path is computed here, vectorised. Doing it in SQL instead means one
pose-by-segment row per pair, which the index aggregates an order of magnitude slower than
the arithmetic itself takes.

The distance is 3D when every point of the path states a height and planar when any omits
it. A path drawn on a floor plan has no height, and a robot's reference frame sits above
the floor, so measuring that frame against height zero would add a constant offset to every
distance.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from robovast.results_processing import index_query

#: Poses per block of the distance computation, so a long track against a long path does
#: not allocate one pose-by-segment matrix for the whole recording.
_BLOCK = 2048


def _lit(value: str) -> str:
    """A SQL string literal."""
    return "'" + str(value).replace("'", "''") + "'"


def choose_path(markers: list[dict], label: str | None) -> dict:
    """The ``path`` marker named *label*, or the only one. Never a guess between several."""
    paths = [m for m in markers if m.get("kind") == "path"]
    if label is not None:
        named = [m for m in paths if m.get("label") == label]
        if len(named) == 1:
            return named[0]
        raise ValueError(
            f"{len(named)} path markers are labelled {label!r}; the path markers are "
            f"{sorted(repr(m.get('label', '')) for m in paths) or 'none'}")
    if len(paths) == 1:
        return paths[0]
    if not paths:
        raise ValueError("this configuration contributes no path marker, so there is no "
                         "path to measure against")
    raise ValueError(
        f"this configuration contributes {len(paths)} path markers; name one with "
        f"marker_label: {sorted(repr(m.get('label', '')) for m in paths)}")


def _distances(poses: np.ndarray, path: np.ndarray) -> np.ndarray:
    """Each pose's distance to the nearest point of the polyline *path* (both ``(n, d)``)."""
    if len(path) == 1:
        path = np.vstack([path, path])
    a, b = path[:-1], path[1:]
    ab = b - a
    length2 = np.einsum("ij,ij->i", ab, ab)
    nearest = np.empty(len(poses))
    for start in range(0, len(poses), _BLOCK):
        block = poses[start:start + _BLOCK, None, :]                   # (m, 1, d)
        ap = block - a[None, :, :]                                     # (m, s, d)
        with np.errstate(invalid="ignore", divide="ignore"):
            u = np.where(length2 > 0, np.einsum("msd,sd->ms", ap, ab) / length2, 0.0)
        u = np.clip(u, 0.0, 1.0)
        gap = ap - u[:, :, None] * ab[None, :, :]
        nearest[start:start + _BLOCK] = np.sqrt(np.einsum("msd,msd->ms", gap, gap).min(axis=1))
    return nearest


def track_deviation(campaign_id: str, config_name: str, run_id: int, *, path: dict,
                    source: str = "poses", frame: str = "base_link") -> dict[str, Any]:
    """Distance from every pose of one track to *path*: ``{points, mean_m, max_m, ...}``.

    Lengths are measured in the dimensions the comparison uses: against a path that states no
    height, both the path and the track are measured in the plane, so the two are comparable.

    Raises ``ValueError`` for a source that is not a pose table, and ``KeyError`` for a
    track with no poses, naming the frames the run did record.
    """
    points = path.get("points") or []
    if not points:
        raise ValueError("the path marker has no points")
    planar = any(len(p) < 3 for p in points)
    dims = 2 if planar else 3
    polyline = np.array([[float(v) for v in p[:dims]] for p in points])

    with index_query.open_index(readonly=True, campaigns=[campaign_id]) as conn:
        tables: dict[str, set] = {}
        for table, column in conn.execute(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema()").fetchall():
            tables.setdefault(table, set()).add(column)
        pose_tables = sorted(t for t, cols in tables.items()
                             if "position.x" in cols and "frame" in cols)
        if source not in pose_tables:
            raise ValueError(f"{source!r} is not a pose table in the index; the pose tables "
                             f"are {', '.join(pose_tables) or 'none'}")
        columns = tables[source]
        if dims == 3 and "position.z" not in columns:
            raise ValueError(f"the path states heights but {source!r} records no position.z, "
                             "so a 3D distance cannot be measured")
        clock = "stamp" if "stamp" in columns else "timestamp"
        z = ', CAST("position.z" AS double precision)' if dims == 3 else ""
        track = (f'FROM "{source}" WHERE campaign_id = {_lit(campaign_id)} '
                 f"AND config_name = {_lit(config_name)} "
                 f"AND CAST(run_id AS integer) = {int(run_id)} AND \"{clock}\" IS NOT NULL")
        frames = [r[0] for r in conn.execute(f"SELECT DISTINCT frame {track}").fetchall()]
        if frame not in frames:
            raise KeyError(f"no {source} poses of frame {frame!r} in run {run_id} of "
                           f"{config_name!r}; recorded frames: "
                           f"{', '.join(sorted(frames)) or 'none'}")
        # Ordered by the measurement clock: the distances do not need an order, the length
        # travelled between consecutive poses does, and a table returns its rows in none.
        rows = conn.execute(
            'SELECT CAST("position.x" AS double precision), '
            f'CAST("position.y" AS double precision){z} {track} AND frame = {_lit(frame)} '
            f'ORDER BY "{clock}"').fetchall()

    track_points = np.array(rows, dtype=float)
    distances = _distances(track_points, polyline)
    path_length = float(np.linalg.norm(np.diff(polyline, axis=0), axis=1).sum())
    track_length = float(np.linalg.norm(np.diff(track_points, axis=0), axis=1).sum())
    return {"campaign_id": campaign_id, "config_name": config_name, "run_id": int(run_id),
            "source": source, "frame": frame, "marker_label": path.get("label", ""),
            "planar": planar, "points": len(distances),
            "mean_m": float(distances.mean()), "max_m": float(distances.max()),
            "path_length_m": path_length, "track_length_m": track_length,
            # How much of the driving the path accounts for. 1.0 is a track as short as the
            # path it followed; below it, the run drove further than the path is long.
            "efficiency": (path_length / track_length) if track_length else None}
