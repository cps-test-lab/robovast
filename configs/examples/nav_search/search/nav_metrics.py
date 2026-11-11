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

It reads the run's tables through :mod:`robovast_data`: ``ground_truth_poses`` (where the robot
truly was, in the world frame), the recorded ``/clearance`` and ``/collision``
topics, and ``nav2_behaviors`` where the run has it.

**This reads clearance; it does not compute it.** Deriving minimum clearance from recorded
poses and footprint radii is the wrong layer, and wrong three ways: the closest approach
falls *between* pose samples (and the faster the pass, the more it misses); a radius round
the base is a calibration constant standing between the geometry and the result; and a
pedestrian's nearest part is whichever limb is extended, which a point-plus-circle cannot
see. The simulator knows the real geometry at 200 Hz, so ``clearance_monitor`` measures it
there and the trial records ``/clearance``. What is left here is arithmetic over recorded
signals.

**The duration is the navigation's, not the recording's.** It runs from the first
behaviour-tree transition -- nav2 accepting the goal -- to the last, both on the tree log's own
clock, and ``nav_start_s`` and ``nav_end_s`` say where on that clock they fell. The recording
starts with the simulator and holds the stack's bring-up before any goal is sent; the trial's
timeout counts from the scenario's start on the same clock, so the extractor reads its time
margin off ``nav_end_s``. A run without a transition has no measured navigation and its
duration, start and end are left empty.

**The goal is each configuration's own.** Arrival is measured against the ``goal_pose`` the
configuration's resolved scenario parameters (``_config/scenario.config``) carry, so a campaign
that varies the goal scores every run against the goal it navigated to. A configuration whose
scenario declares no goal pose is refused by name rather than measured against a default.

**``collided`` still comes from the oracle, not from the distance.** The two disagree in
one direction: a fast pass can touch between two *published* clearance samples even when
each sample is exact. ``contact_monitor`` reports a real contact force, so it decides
whether a run failed; clearance only grades how close it came.
"""

import csv
import math
from pathlib import Path
from typing import Tuple

from robovast.common.campaign_data import read_scenario_config
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


def goal_of(config_dir: Path) -> Tuple[float, float]:
    """The goal this configuration's runs navigated to, from its resolved scenario parameters."""
    try:
        scenario = read_scenario_config(config_dir)
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"{config_dir}: no resolved scenario parameters (_config/scenario.config), so the "
            f"goal its runs navigated to is unknown and arrival cannot be measured") from exc
    position = ((scenario or {}).get('goal_pose') or {}).get('position') or {}
    try:
        return float(position['x']), float(position['y'])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            f"{config_dir}: the resolved scenario parameters carry no goal_pose.position with x "
            f"and y. Arrival is measured against the goal each configuration navigated to, so the "
            f"campaign has to state goal_pose as a scenario parameter") from exc


def _metrics_for_run(run_dir: Path, poses_table: str, goal) -> dict | None:
    tables = _RunTables(run_dir)
    if poses_table not in tables.present:
        return None
    poses = tables.rows(poses_table)
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
            f"recorded (execution.recording.ros2.topics in the .vast) and in rosbags_to_csv's "
            f"topics?")
    collided = any(_true(r.get('data')) for r in tables.rows(collision))

    # Optional, unlike the two above: a run that needed no recovery behaviour records no
    # recovery transitions, and `recovery_count` is a QD measure rather than part of the
    # verdict. So an absent table means zero recoveries -- which is what happened -- and not
    # a defect.
    behaviours = tables.rows(tables.first(_BEHAVIOR_TABLES))
    recoveries = sum(
        1 for r in behaviours
        if any(k in str(r.get('behavior_name') or '').lower()
               for k in ('spin', 'backup', 'wait', 'clear'))
        and str(r.get('status_name') or '').upper().startswith('RUNNING'))

    # The navigation's duration is the behaviour tree's: from its first transition, nav2
    # accepting the goal, to its last, the tree finishing or the trial ending it. Both ends on
    # one clock, the tree log's own. The recording starts with the simulator, so the pose
    # track's first sample is bring-up -- the stack launching, the initial pose being set --
    # which the trial's own timeout does not count; and the pose track is not the tree's
    # clock, so its last sample is no end for a span that starts in the tree log. A run with
    # no transition has no measured navigation: its duration is left empty rather than
    # measured from the wrong moments, and the extractor drops that margin and says so. The
    # baseline rows the tree plugin writes for nodes that never ran carry INVALID and are not
    # transitions.
    transitions = _floats(
        [r for r in behaviours if str(r.get('status_name') or '').upper() != 'INVALID'],
        'timestamp')

    return {
        'min_clearance': round(min(clearances), 4) if clearances else '',
        'duration_s': round(max(transitions) - min(transitions), 3) if transitions else '',
        # Where on the recording's clock the navigation began and ended: the trial's own
        # timeout runs on that clock from the scenario's start, so the time margin is read
        # off `nav_end_s`, not off the span.
        'nav_start_s': round(min(transitions), 3) if transitions else '',
        'nav_end_s': round(max(transitions), 3) if transitions else '',
        'final_distance_to_goal': round(math.hypot(last[0] - goal[0], last[1] - goal[1]), 4),
        'collided': int(collided),
        'recovery_count': recoveries,
        'path_end_x': round(last[0], 4),
        'path_end_y': round(last[1], 4),
    }


class NavMetrics(BasePostprocessingPlugin):
    """Derive ``nav_metrics.csv`` per run from its ground truth and the recorded oracles.

    A run that already has its file is left as it is unless *force*: a run's recording does
    not change once the run has ended.
    """

    def __call__(self, results_dir: str, config_dir: str,
                 poses: str = 'ground_truth_poses', file: str = 'nav_metrics.csv',
                 force: bool = False, provenance_file: str | None = None,
                 debug: bool = False) -> Tuple[bool, str]:
        # No **kwargs: a parameter this plugin does not take fails by name instead of being
        # ignored. The goal is the configuration's, so a goal_x/goal_y stated here is refused.
        del config_dir  # every input is per-run or per-configuration: nothing is campaign-wide
        del provenance_file, debug  # passed by postprocessing to every plugin; unused here
        written = skipped = missing = no_clearance = 0
        goals: dict[str, Tuple[float, float]] = {}
        runs = Campaign(results_dir).runs
        for config_name, run_id in runs.loc[runs['run_id'].notna(),
                                            ['config_name', 'run_id']].itertuples(index=False):
            cfg_dir = Path(results_dir) / str(config_name)
            run_dir = cfg_dir / str(int(run_id))
            if not run_dir.is_dir():
                continue
            out = run_dir / file
            if not force and out.exists():
                skipped += 1
                continue
            if config_name not in goals:
                goals[config_name] = goal_of(cfg_dir)
            metrics = _metrics_for_run(run_dir, poses, goals[config_name])
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
            note += f"; {missing} run(s) had no ground-truth track in {poses}"
        if no_clearance:
            note += (f"; {no_clearance} run(s) recorded no /clearance -- is clearance_monitor "
                     f"in the world and the topic recorded (execution.recording.ros2.topics)?")
        return True, note
