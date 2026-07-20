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
        return {
            "t": float(row["timestamp"]),
            "frame_id": row["frame_id"],
            "resolution": float(row["resolution"]),
            "width": int(row["width"]),
            "height": int(row["height"]),
            "origin_x": float(row["origin_x"]),
            "origin_y": float(row["origin_y"]),
            "origin_yaw": float(row["origin_yaw"]),
            "data": row["data"],
        }
