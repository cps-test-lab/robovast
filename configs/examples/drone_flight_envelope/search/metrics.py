# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Flight-envelope metrics: what one drone trial did, derived from its odometry.

Per *run*, reads the ``/drone/odom`` CSV that ``rosbags_to_csv`` extracted from the trial bag and
writes two files beside it:

``trajectory.csv``
    The flight path, one row per odometry sample, in the shape the run-view panels bind to
    (``t``, ``x``, ``y``, ``z``, ``tilt_deg``, ``speed``). This is what makes the campaign
    *visible*: the ground-track and timeseries panels read it directly.

``metrics.csv``
    One row of scalars: the run's summary, read by the notebooks, the data browser, and the search
    extractor.

Used as a local-file postprocessing plugin (``search/metrics.py:EnvelopeMetrics``) under
``results_processing.postprocessing`` (batch) or ``search.postprocessing`` (search). Run discovery
globs, so it works pointed at a campaign root or a single run.
"""

from __future__ import annotations

import csv
import math
from pathlib import Path
from typing import Tuple

from robovast.results_processing.postprocessing_plugins import \
    BasePostprocessingPlugin

#: Commanded cruise altitude, and the course the scenario flies. Restated here rather than parsed
#: out of the .osc because a metric needs to know what "on target" meant; if the scenario's course
#: changes, this changes with it.
CRUISE_Z = 1.2
COURSE = [(1.2, 1.2), (-1.2, 1.2), (-1.2, -1.2), (0.0, 0.0)]

#: The cruise window, in seconds from the first odometry sample: after the climb has settled and
#: **before** the descent is commanded. Both ends matter. Without the lower bound the climb counts
#: as failure to hold altitude; without the upper bound the commanded landing does, and since every
#: run lands, `cruise_hold_fraction` then reads ~0.81 in every cell no matter what was varied --
#: a metric that looks like a measurement and discriminates nothing.
CRUISE_START_S = 6.0
CRUISE_END_S = 38.0

#: Altitude below which the drone is on the floor rather than flying.
GROUNDED_Z = 0.15
#: Fraction of the commanded cruise altitude that still counts as holding it, for the
#: `cruise_hold_fraction` diagnostic.
HOLD_FRACTION = 0.75
#: Mean altitude error, in metres, up to which the drone counts as having held station. 0.15 m is
#: five times the airframe's own height -- generous, but a band you can see it leave.
#:
#: The outcome is cut on mean altitude error rather than on `cruise_hold_fraction`, which is kept
#: only as a diagnostic. Measured across the campaign's grid, that fraction is 1.0 in every cell:
#: even the heaviest configuration stays above 0.75 of the commanded altitude, so a threshold on it
#: separates nothing. Mean error over the same window runs 0.0005 -> 0.12 -> 0.24 m across the
#: payload levels, which is the quantity the physics actually moves.
HOLD_TOLERANCE_M = 0.15


def _quat_tilt_deg(x: float, y: float, z: float, w: float) -> float:
    """Angle between the body z axis and vertical, in degrees.

    From the quaternion rather than from roll/pitch: Euler angles are lossy the moment a body leaves
    the plane, and a quadrotor rejecting a gust is exactly that case. The body z axis in world
    coordinates is the third column of the rotation matrix, so the tilt is its angle to (0, 0, 1).
    """
    body_z = 1.0 - 2.0 * (x * x + y * y)
    return math.degrees(math.acos(max(-1.0, min(1.0, body_z))))


def _read_odom(path: Path) -> list[dict]:
    """The odometry CSV as t-relative samples. Returns [] when the bag had no messages."""
    rows = []
    with open(path, newline="") as handle:
        for row in csv.DictReader(handle):
            try:
                rows.append(
                    {
                        "t": float(row["timestamp"]) * 1e-9,
                        "x": float(row["pose.pose.position.x"]),
                        "y": float(row["pose.pose.position.y"]),
                        "z": float(row["pose.pose.position.z"]),
                        "qx": float(row["pose.pose.orientation.x"]),
                        "qy": float(row["pose.pose.orientation.y"]),
                        "qz": float(row["pose.pose.orientation.z"]),
                        "qw": float(row["pose.pose.orientation.w"]),
                        "vx": float(row["twist.twist.linear.x"]),
                        "vy": float(row["twist.twist.linear.y"]),
                        "vz": float(row["twist.twist.linear.z"]),
                    }
                )
            except (KeyError, ValueError, TypeError):
                # A malformed row is skipped rather than failing the run: one bad sample must not
                # discard an otherwise complete flight.
                continue
    if not rows:
        return []
    t0 = rows[0]["t"]
    for row in rows:
        row["t"] -= t0
    return rows


def _trajectory(rows: list[dict]) -> list[dict]:
    out = []
    for row in rows:
        out.append(
            {
                "t": round(row["t"], 4),
                "x": round(row["x"], 4),
                "y": round(row["y"], 4),
                "z": round(row["z"], 4),
                "tilt_deg": round(_quat_tilt_deg(row["qx"], row["qy"], row["qz"], row["qw"]), 3),
                "speed": round(math.sqrt(row["vx"] ** 2 + row["vy"] ** 2 + row["vz"] ** 2), 4),
            }
        )
    return out


def _outcome(traj: list[dict], mean_altitude_error: float) -> str:
    """One label for the whole flight, in the vocabulary the envelope is drawn in.

    Three outcomes rather than pass/fail, because the interesting part of this campaign is *how* a
    configuration fails. ``could_not_hover`` and ``sagged`` are both "did not fly the commanded
    course" and completely different physics: the first is a thrust-to-weight ratio at or below 1,
    the second is a position loop with no integral term trading altitude for weight.
    """
    if max(r["z"] for r in traj) < GROUNDED_Z:
        return "could_not_hover"
    if mean_altitude_error > HOLD_TOLERANCE_M:
        return "sagged"
    return "held"


def _metrics(rows: list[dict]) -> dict:
    traj = _trajectory(rows)
    duration = traj[-1]["t"] if traj else 0.0

    # Cruise phase: the commanded course, with the climb and the landing excluded, so that a drone
    # which never climbs is measured over the same window as one that does.
    cruising = [r for r in traj if CRUISE_START_S <= r["t"] < CRUISE_END_S]
    if not cruising:
        cruising = traj
    held = [r for r in cruising if r["z"] >= HOLD_FRACTION * CRUISE_Z]
    cruise_hold = len(held) / len(cruising) if cruising else 0.0

    altitude_error = [abs(r["z"] - CRUISE_Z) for r in cruising]
    mean_altitude_error = sum(altitude_error) / len(altitude_error) if altitude_error else 0.0

    # Distance from the nearest commanded corner, as a crude tracking error that does not need the
    # leg schedule: the course is a square, so the nearest corner is the one being flown to or from.
    tracking = []
    for r in cruising:
        tracking.append(min(math.dist((r["x"], r["y"]), corner) for corner in COURSE))
    tracking_rmse = math.sqrt(sum(e * e for e in tracking) / len(tracking)) if tracking else 0.0

    landed = traj[-1]
    return {
        "outcome": _outcome(traj, mean_altitude_error),
        "cruise_hold_fraction": round(cruise_hold, 4),
        "mean_altitude_error": round(mean_altitude_error, 4),
        "tracking_rmse": round(tracking_rmse, 4),
        "max_tilt_deg": round(max(r["tilt_deg"] for r in traj), 3),
        "max_speed": round(max(r["speed"] for r in traj), 4),
        "landing_error": round(math.dist((landed["x"], landed["y"]), (0.0, 0.0)), 4),
        "final_z": round(landed["z"], 4),
        "duration_s": round(duration, 3),
    }


def _write_csv(path: Path, rows: list[dict]) -> None:
    with open(path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


class EnvelopeMetrics(BasePostprocessingPlugin):
    def __call__(self, results_dir: str, config_dir: str,
                 odom_glob: str = "*drone_odom.csv", trajectory: str = "trajectory.csv",
                 file: str = "metrics.csv", force: bool = False, **kwargs) -> Tuple[bool, str]:
        written = skipped = empty = 0
        for odom in sorted(Path(results_dir).rglob(odom_glob)):
            run_dir = odom.parent
            out = run_dir / file
            # Incremental: a re-run over the whole campaign only touches new runs. `force` redoes all.
            if not force and out.exists() and out.stat().st_mtime >= odom.stat().st_mtime:
                skipped += 1
                continue
            rows = _read_odom(odom)
            if not rows:
                # A run that produced no odometry is reported, not silently skipped: it means the
                # simulator or the bridge never came up, which is a different problem from a drone
                # that flew badly.
                empty += 1
                continue
            _write_csv(run_dir / trajectory, _trajectory(rows))
            metrics = _metrics(rows)
            _write_csv(out, [metrics])
            written += 1
        notes = []
        if skipped:
            notes.append(f"{skipped} up-to-date")
        if empty:
            notes.append(f"{empty} with no odometry")
        suffix = f" ({', '.join(notes)})" if notes else ""
        return True, f"EnvelopeMetrics wrote {file} for {written} run(s){suffix}"
