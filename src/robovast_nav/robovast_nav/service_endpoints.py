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

"""Package-provided web-service data endpoints shipped by ``robovast_nav``.

A ``robovast.service_endpoints`` plugin serves run-scoped JSON at
``GET /campaigns/{id}/<name>?config_name=…&run_id=…&…`` with no core edit — see
:mod:`robovast.service.endpoint_plugin` for the mechanism. This is the data half of the
nav costmap panel: the panel (``robovast_nav/web``) calls ``data.fetchRun('costmap', …)``,
which lands here. Panel + endpoint both now live in this package.
"""


class CostmapEndpoint:
    """Serve the nav2 costmap frame nearest a time, for the run-view costmap panel.

    Reads the ``costmaps`` table (produced by the ``rosbags_costmap_to_csv`` postprocessing
    step) directly and untruncated — the panel decodes ``data`` (zlib+base64 int8 cells).
    The bare name ``costmap`` matches the existing panel's ``fetchRun('costmap', …)`` call;
    new packages should namespace their endpoints (e.g. ``nav/foo``).

    Alongside the frame it reports **``t_prev`` and ``t_next``** — the timestamps recorded
    either side of it for the same topic, ``None`` at the ends of the topic's span. "Nearest"
    on its own is not an interpretable answer: this query always returns *something*, however
    far from ``t``, so a caller cannot tell a current frame from the first or last one clamped
    to a cursor minutes away. The pair is the minimum that makes it interpretable, and the
    panel derives two things from it (``@robovast/panel-kit``'s ``frameValidity``):

    * the interval over which this frame stays the nearest one, so it re-requests only when
      the answer could actually change (a latched topic such as ``/map`` has one row, hence
      no neighbours, hence is fetched once for the whole session); and
    * the local publish period, hence how far the cursor may drift before the frame should be
      reported as absent rather than drawn as current.

    Deliberately *not* a ``tolerance`` query param that returns nothing outside it: that would
    make the caller invent the threshold anyway, and would collapse "this topic was never
    recorded" and "nothing was recorded near this time" into the same empty answer.
    """

    name = "costmap"

    def handle(self, ctx):
        from robovast.results_processing.data_query import DataQueryError

        topic = ctx.params.get("topic")
        if not topic:
            raise ValueError("missing required query param 'topic'")
        t_raw = ctx.params.get("t")
        try:
            t = float(t_raw)
        except (TypeError, ValueError) as e:
            raise ValueError(f"t must be a number, got {t_raw!r}") from e

        with ctx.open_db() as conn:
            has = conn.execute(
                "SELECT 1 FROM main.sqlite_master WHERE type='table' AND name='costmaps'"
            ).fetchone()
            if not has:
                raise DataQueryError(
                    "No 'costmaps' table — add the rosbags_costmap_to_csv postprocessing "
                    "step (and record the costmap topics in the scenario's bag_record).")
            row = conn.execute(
                'SELECT timestamp, frame_id, resolution, width, height, '
                'origin_x, origin_y, origin_yaw, data FROM costmaps '
                'WHERE config_name = ? AND run_id = ? AND topic = ? '
                'ORDER BY ABS(CAST(timestamp AS REAL) - ?) LIMIT 1',
                (ctx.config_name, ctx.run_id, topic, t)).fetchone()
            if row is None:
                return None
            # The frame's recorded neighbours (see the class docstring). CAST is load-bearing, not
            # cosmetic: `timestamp` is REAL in a typed-ingest data.db but TEXT in an older one, and
            # MIN/MAX/</> over TEXT compare lexicographically ('10.022' < '9.5'), so an uncast query
            # would return a plausible wrong neighbour on exactly the databases still supported.
            t_frame = float(row["timestamp"])
            neighbours = conn.execute(
                'SELECT MAX(CAST(timestamp AS REAL)) AS t_prev, '
                '(SELECT MIN(CAST(timestamp AS REAL)) FROM costmaps '
                ' WHERE config_name = ? AND run_id = ? AND topic = ? '
                ' AND CAST(timestamp AS REAL) > ?) AS t_next '
                'FROM costmaps WHERE config_name = ? AND run_id = ? AND topic = ? '
                'AND CAST(timestamp AS REAL) < ?',
                (ctx.config_name, ctx.run_id, topic, t_frame,
                 ctx.config_name, ctx.run_id, topic, t_frame)).fetchone()

        t_prev = neighbours["t_prev"]
        t_next = neighbours["t_next"]
        return {
            "t": t_frame,
            "t_prev": None if t_prev is None else float(t_prev),
            "t_next": None if t_next is None else float(t_next),
            "frame_id": row["frame_id"],
            "resolution": float(row["resolution"]),
            "width": int(row["width"]),
            "height": int(row["height"]),
            "origin_x": float(row["origin_x"]),
            "origin_y": float(row["origin_y"]),
            "origin_yaw": float(row["origin_yaw"]),
            "data": row["data"],
        }
