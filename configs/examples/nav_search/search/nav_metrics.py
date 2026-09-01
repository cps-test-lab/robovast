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

Writes ``nav_metrics.csv`` beside each run. The search extractor reads it and so do the
analysis notebooks -- one place computes a metric, which is what keeps the number in a
figure the same as the number a search optimised.

**This reads clearance; it does not compute it.** An earlier version of this file derived
minimum clearance here, from recorded poses and hardcoded footprint radii. That was the
wrong layer and it was wrong three ways: the closest approach falls *between* pose samples
(and the faster the pass, the more it misses); a radius round the base is a calibration
constant standing between the geometry and the result; and a pedestrian's nearest part is
whichever limb is extended, which a point-plus-circle cannot see. The simulator knows the
real geometry at 200 Hz, so ``clearance_monitor`` measures it there and the trial records
``/clearance``. What is left here is arithmetic over recorded signals.

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


#: What ``rosbags_to_csv`` names a topic's table, and the plain name a hand-written test or
#: another converter might use. Tried in order. Recorded rather than guessed: the pilot wrote
#: ``rosbag2_clearance.csv`` while this plugin read ``clearance.csv``, so every run reported an
#: empty clearance AND a `collided` of 0 that no file had supplied -- a fabricated measurement
#: that looked exactly like a clean crossing.
_CLEARANCE_FILES = ('rosbag2_clearance.csv', 'clearance.csv')
_COLLISION_FILES = ('rosbag2_collision.csv', 'collision.csv')
_BEHAVIOR_FILES = ('nav2_behaviors.csv', 'behaviors.csv')


def _rows(path: Path | None) -> list[dict]:
    """Every row of *path*, or ``[]`` when there is no such table.

    ``None`` is accepted because every caller passes a :func:`_first` result, which is
    ``None`` for a table this run does not have. A guard at the call site is the obvious
    alternative and it is the one that failed: written inside a generator expression, where
    the source is evaluated before any condition runs, it never guarded anything.
    """
    if path is None or not path.exists():
        return []
    with open(path, newline='', encoding='utf-8') as handle:
        return list(csv.DictReader(handle))


def _first(run_dir: Path, names) -> Path | None:
    """The first of ``names`` that exists in this run, or ``None``."""
    for name in names:
        path = run_dir / name
        if path.exists():
            return path
    return None


def _floats(rows: list[dict], *keys) -> list[float]:
    """Every parseable value under the first key that any row actually carries.

    Tolerant about WHICH column holds it because the CSV a topic converts to is named by
    the message field, and a Float32 lands under ``data`` while a structured payload lands
    under its own name. Not tolerant about it being absent -- see the caller.
    """
    out = []
    for row in rows:
        for key in keys:
            if key in row:
                try:
                    out.append(float(row[key]))
                except (TypeError, ValueError):
                    pass
                break
    return out


def _metrics_for_run(run_dir: Path, poses_file: str, gt_frame: str, goal) -> dict | None:
    poses = [r for r in _rows(run_dir / poses_file)
             if gt_frame in (r.get('frame') or '')]
    if not poses:
        # No ground-truth track: nothing here can be computed, and a row of zeros would be
        # indistinguishable from a robot that never moved.
        return None

    times, last = [], None
    for row in poses:
        try:
            times.append(float(row['timestamp']))
            last = (float(row['position.x']), float(row['position.y']))
        except (KeyError, TypeError, ValueError):
            continue
    if last is None:
        return None

    # The recorded clearance series. Empty means the world ran no clearance_monitor, which
    # is a configuration mistake rather than an infinitely safe run -- reported as an empty
    # cell so the extractor drops that margin instead of scoring a fabricated one.
    clearance_csv = _first(run_dir, _CLEARANCE_FILES)
    clearances = _floats(_rows(clearance_csv), 'data', 'current', 'clearance')

    collision_csv = _first(run_dir, _COLLISION_FILES)
    if collision_csv is None:
        # The oracle's table is missing, and `collided = False` here would be a fabricated
        # measurement indistinguishable from a clean crossing -- the single most misleading
        # value this plugin could write, because every downstream consumer trusts it for the
        # verdict. Refuse the run instead; the extractor then records the cell as unmeasured.
        raise FileNotFoundError(
            f"{run_dir}: no collision table ({' or '.join(_COLLISION_FILES)}). Is /collision "
            f"in the scenario's bag_record and in rosbags_to_csv's topics?")
    collided = any((r.get('data') or '').strip().lower() in ('true', '1')
                   for r in _rows(collision_csv))

    # Optional, unlike the two above: a run that needed no recovery behaviour records no
    # transitions, and `recovery_count` is a QD measure rather than part of the verdict. So
    # an absent table means zero recoveries -- which is what happened -- and not a defect.
    recoveries = sum(
        1 for r in _rows(_first(run_dir, _BEHAVIOR_FILES))
        if any(k in (r.get('behavior_name') or '').lower()
               for k in ('spin', 'backup', 'wait', 'clear'))
        and (r.get('status_name') or '').upper().startswith('RUNNING'))

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
    """Derive ``nav_metrics.csv`` per run from the pose table and the recorded oracles."""

    def __call__(self, results_dir: str, config_dir: str,
                 poses: str = 'poses.csv', file: str = 'nav_metrics.csv',
                 gt_frame: str = '_gt', goal_x: float = 2.5, goal_y: float = 0.0,
                 force: bool = False, **kwargs) -> Tuple[bool, str]:
        del config_dir  # every input is per-run now that clearance is recorded, not derived
        written = skipped = missing = no_clearance = 0
        for pose_csv in sorted(Path(results_dir).rglob(poses)):
            run_dir = pose_csv.parent
            out = run_dir / file
            if not force and out.exists() and out.stat().st_mtime >= pose_csv.stat().st_mtime:
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
                     f"in the world and the topic in bag_record?")
        return True, note
