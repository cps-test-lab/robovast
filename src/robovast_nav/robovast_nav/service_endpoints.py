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


#: Why a campaign has no costmap to show, and what to do about it. One message for both
#: ways it happens, because the remedy is the same and the endpoint cannot tell them apart:
#: the stack may be nav2-on-ROS 2 with the postprocessing step simply not declared, or it may
#: not be nav2 at all -- a differently-navigated robot, or one on another middleware, has no
#: costmap topic to record and never will. Naming both is what stops the second case reading
#: as a misconfiguration of the first.
_ABSENT = (
    "This campaign recorded no nav2 costmaps. If the stack is nav2 on ROS 2, add the "
    "rosbags_costmap_to_csv postprocessing step and record the costmap topics in the "
    "scenario's bag_record. If it is not -- another planner, another middleware, or a robot "
    "that does not navigate -- this panel has nothing to show for it.")


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
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name = 'costmaps'"
            ).fetchone()
            if not has:
                raise DataQueryError(_ABSENT)
            row = conn.execute(
                'SELECT timestamp, frame_id, resolution, width, height, '
                'origin_x, origin_y, origin_yaw, data FROM costmaps '
                'WHERE campaign_id = %s AND config_name = %s AND run_id = %s AND topic = %s '
                'ORDER BY ABS(CAST(timestamp AS double precision) - %s) LIMIT 1',
                (ctx.campaign_id, ctx.config_name, ctx.run_id, topic, t)).fetchone()
            if row is None:
                # Absent and empty are different answers, and the central index makes the
                # difference matter. The table existing says only that *some* campaign in the
                # index is a nav2/ROS 2 stack that recorded costmaps -- so a campaign whose
                # robot has no nav2, or runs on another middleware entirely, would fall
                # through to a bare `None` that the panel draws as "no frame here", implying
                # a costmap exists elsewhere in the run. This query has no time bound, so no
                # row means this run recorded no costmaps at all: say so, and name the fix.
                scoped = conn.execute(
                    'SELECT 1 FROM costmaps WHERE campaign_id = %s LIMIT 1',
                    (ctx.campaign_id,)).fetchone()
                if not scoped:
                    raise DataQueryError(_ABSENT)
                return None
            # The frame's recorded neighbours (see the class docstring). Two things are
            # load-bearing here, not cosmetic. `campaign_id` scopes the query: one index holds
            # every campaign, so without it this matches run 3 of config 'nominal' in every
            # campaign that has one. And the CAST is `double precision`, not `REAL`: `timestamp`
            # may be text, and MIN/MAX/</> over text compare lexicographically ('10.022' <
            # '9.5'); but Postgres' REAL is 4 bytes, which rounds an epoch stamp to the nearest
            # ~30 s and would pick a neighbour tens of seconds away while still looking sane.
            t_frame = float(row["timestamp"])
            neighbours = conn.execute(
                'SELECT MAX(CAST(timestamp AS double precision)) AS t_prev, '
                '(SELECT MIN(CAST(timestamp AS double precision)) FROM costmaps '
                ' WHERE campaign_id = %s AND config_name = %s AND run_id = %s AND topic = %s '
                ' AND CAST(timestamp AS double precision) > %s) AS t_next '
                'FROM costmaps WHERE campaign_id = %s AND config_name = %s AND run_id = %s '
                'AND topic = %s AND CAST(timestamp AS double precision) < %s',
                (ctx.campaign_id, ctx.config_name, ctx.run_id, topic, t_frame,
                 ctx.campaign_id, ctx.config_name, ctx.run_id, topic, t_frame)).fetchone()

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
