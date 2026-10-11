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

"""Per-run navigation metrics: how close, how long, how it ended.

Writes ``nav_metrics.csv`` beside each run -- the ``nav_metrics`` table. The search extractor
reads it and so do the analysis notebooks -- one place computes a metric, which is what keeps
the number in a figure the same as the number a search optimised.

It reads the run's tables through :mod:`robovast_data`: the pose table, the recorded
``/clearance`` and ``/collision`` topics, and ``nav2_behaviors`` where the run has it.

**This reads clearance; it does not compute it.** Deriving minimum clearance from recorded
poses and footprint radii is the wrong layer, and wrong three ways: the closest approach
falls *between* pose samples (and the faster the pass, the more it misses); a radius round
the base is a calibration constant standing between the geometry and the result; and a
pedestrian's nearest part is whichever limb is extended, which a point-plus-circle cannot
see. The simulator knows the real geometry at 200 Hz, so ``clearance_monitor`` measures it
there and the trial records ``/clearance``. What is left here is arithmetic over recorded
signals.

**``collided`` still comes from the oracle, not from the distance.** The two disagree in
one direction: a fast pass can touch between two *published* clearance samples even when
each sample is exact. ``contact_monitor`` reports a real contact force, so it decides
whether a run failed; clearance only grades how close it came.
"""

import csv
import math
from pathlib import Path
from typing import Tuple

from robovast.results_processing.postprocessing_plugins import \
    BasePostprocessingPlugin
from robovast_data import Campaign, open_data

#: A topic's table as ``rosbags_to_csv`` names it, then the plain name a run's own file
#: would give it. Tried in order; a run with neither is named, never read as empty.
_CLEARANCE_TABLES = ('rosbag2_clearance', 'clearance')
_COLLISION_TABLES = ('rosbag2_collision', 'collision')
_BEHAVIOR_TABLES = ('nav2_behaviors', 'behaviors')


class _RunTables:
    """One run's tables: which it has, and their rows."""

    def __init__(self, run_dir: Path):
        self.data = open_data(str(run_dir))
        tables = self.data.tables
        self.present = set(tables.loc[tables['runs'].fillna(0) > 0, 'name'])

    def first(self, names) -> str | None:
        """The first of ``names`` this run has, or ``None``."""
        return next((name for name in names if name in self.present), None)

    def rows(self, name: str | None) -> list[dict]:
        """Every row of table *name*, or ``[]`` when there is no such table (``None``)."""
        if name is None:
            return []
        return self.data.table(name).to_dict('records')


def _floats(rows: list[dict], *keys) -> list[float]:
    """Every parseable value under the first key that any row actually carries.

    Tolerant about WHICH column holds it because a topic's table is named by the message
    field, and a Float32 lands under ``data`` while a structured payload lands under its own
    name. Not tolerant about it being absent -- see the caller.
    """
    out = []
    for row in rows:
        for key in keys:
            if key in row:
                try:
                    value = float(row[key])
                except (TypeError, ValueError):
                    break
                if not math.isnan(value):
                    out.append(value)
                break
    return out


def _true(value) -> bool:
    return str(value).strip().lower() in ('true', '1', '1.0')


def _metrics_for_run(run_dir: Path, poses_table: str, gt_frame: str, goal) -> dict | None:
    tables = _RunTables(run_dir)
    if poses_table not in tables.present:
        return None
    poses = [r for r in tables.rows(poses_table) if gt_frame in str(r.get('frame') or '')]
    if not poses:
        # No ground-truth track: nothing here can be computed, and a row of zeros would be
        # indistinguishable from a robot that never moved.
        return None

    times, last = [], None
    for row in poses:
        try:
            t, x, y = (float(row['timestamp']), float(row['position.x']),
                       float(row['position.y']))
        except (KeyError, TypeError, ValueError):
            continue
        if math.isnan(t) or math.isnan(x) or math.isnan(y):
            continue
        times.append(t)
        last = (x, y)
    if last is None:
        return None

    # The recorded clearance series. Empty means the world ran no clearance_monitor, which
    # is a configuration mistake rather than an infinitely safe run -- reported as an empty
    # cell so the extractor drops that margin instead of scoring a fabricated one.
    clearances = _floats(tables.rows(tables.first(_CLEARANCE_TABLES)),
                         'data', 'current', 'clearance')

    collision = tables.first(_COLLISION_TABLES)
    if collision is None:
        # The oracle's table is missing, and `collided = False` here would be a fabricated
        # measurement indistinguishable from a clean crossing -- the single most misleading
        # value this plugin could write, because every downstream consumer trusts it for the
        # verdict. Refuse the run instead; the extractor then records the cell as unmeasured.
        raise FileNotFoundError(
            f"{run_dir}: no collision table ({' or '.join(_COLLISION_TABLES)}). Is /collision "
            f"recorded (recording.ros2.topics in the .vast) and in rosbags_to_csv's topics?")
    collided = any(_true(r.get('data')) for r in tables.rows(collision))

    # Optional, unlike the two above: a run that needed no recovery behaviour records no
    # transitions, and `recovery_count` is a QD measure rather than part of the verdict. So
    # an absent table means zero recoveries -- which is what happened -- and not a defect.
    recoveries = sum(
        1 for r in tables.rows(tables.first(_BEHAVIOR_TABLES))
        if any(k in str(r.get('behavior_name') or '').lower()
               for k in ('spin', 'backup', 'wait', 'clear'))
        and str(r.get('status_name') or '').upper().startswith('RUNNING'))

    return {
        'min_clearance': round(min(clearances), 4) if clearances else '',
        'duration_s': round(max(times) - min(times), 3) if times else 0.0,
        'final_distance_to_goal': round(math.hypot(last[0] - goal[0], last[1] - goal[1]), 4),
        'collided': int(collided),
        'recovery_count': recoveries,
        'path_end_x': round(last[0], 4),
        'path_end_y': round(last[1], 4),
    }


class NavMetrics(BasePostprocessingPlugin):
    """Derive ``nav_metrics.csv`` per run from the pose table and the recorded oracles.

    A run that already has its file is left as it is unless *force*: a run's recording does
    not change once the run has ended.
    """

    def __call__(self, results_dir: str, config_dir: str,
                 poses: str = 'poses', file: str = 'nav_metrics.csv',
                 gt_frame: str = '_gt', goal_x: float = 2.5, goal_y: float = 0.0,
                 force: bool = False, **kwargs) -> Tuple[bool, str]:
        del config_dir  # every input is per-run: clearance is recorded, not derived
        written = skipped = missing = no_clearance = 0
        runs = Campaign(results_dir).runs
        for config_name, run_id in runs.loc[runs['run_id'].notna(),
                                            ['config_name', 'run_id']].itertuples(index=False):
            run_dir = Path(results_dir) / str(config_name) / str(int(run_id))
            if not run_dir.is_dir():
                continue
            out = run_dir / file
            if not force and out.exists():
                skipped += 1
                continue
            metrics = _metrics_for_run(run_dir, poses, gt_frame, (goal_x, goal_y))
            if metrics is None:
                missing += 1
                continue
            if metrics['min_clearance'] == '':
                no_clearance += 1
            with open(out, 'w', newline='', encoding='utf-8') as handle:
                writer = csv.writer(handle)
                writer.writerow(list(metrics))
                writer.writerow([metrics[k] for k in metrics])
            written += 1

        note = f"NavMetrics wrote {file} for {written} run(s)"
        if skipped:
            note += f" ({skipped} up-to-date)"
        # Both counted and named. A campaign whose runs quietly stopped being measurable
        # would otherwise look like one whose cells all happened to score the same -- which
        # is exactly what a search would then optimise.
        if missing:
            note += f"; {missing} run(s) had no '{gt_frame}' pose track"
        if no_clearance:
            note += (f"; {no_clearance} run(s) recorded no /clearance -- is clearance_monitor "
                     f"in the world and the topic recorded (recording.ros2.topics)?")
        return True, note
