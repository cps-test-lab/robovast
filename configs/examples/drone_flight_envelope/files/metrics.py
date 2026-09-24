# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Per-run flight metrics from the /drone/odom CSV: trajectory.csv (what the panels bind to) and
metrics.csv (one row of scalars)."""

from __future__ import annotations

import csv
import math
from pathlib import Path
from typing import Tuple

from robovast.results_processing.postprocessing_plugins import \
    BasePostprocessingPlugin

# Must match scenario.osc.
CRUISE_Z = 1.2
COURSE = [(1.2, 1.2), (-1.2, 1.2), (-1.2, -1.2), (0.0, 0.0)]

# Climb settled .. descent commanded. Without both bounds the climb and the landing count as
# failure to hold altitude in every cell alike.
CRUISE_START_S = 6.0
CRUISE_END_S = 38.0

GROUNDED_Z = 0.15
HOLD_FRACTION = 0.75
# The outcome is cut on mean altitude error; cruise_hold_fraction is 1.0 in every cell of the grid
# and separates nothing, so it is kept only as a diagnostic.
HOLD_TOLERANCE_M = 0.15


def _quat_tilt_deg(x: float, y: float, z: float, w: float) -> float:
    """Angle between the body z axis and vertical: the third column of the rotation matrix."""
    body_z = 1.0 - 2.0 * (x * x + y * y)
    return math.degrees(math.acos(max(-1.0, min(1.0, body_z))))


def _read_odom(path: Path) -> list[dict]:
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
    """could_not_hover is T/W <= 1; sagged is a position loop without integral action trading
    altitude for weight. Different physics, so not folded into pass/fail."""
    if max(r["z"] for r in traj) < GROUNDED_Z:
        return "could_not_hover"
    if mean_altitude_error > HOLD_TOLERANCE_M:
        return "sagged"
    return "held"


def _metrics(rows: list[dict]) -> dict:
    traj = _trajectory(rows)
    duration = traj[-1]["t"] if traj else 0.0

    cruising = [r for r in traj if CRUISE_START_S <= r["t"] < CRUISE_END_S]
    if not cruising:
        cruising = traj
    held = [r for r in cruising if r["z"] >= HOLD_FRACTION * CRUISE_Z]
    cruise_hold = len(held) / len(cruising) if cruising else 0.0

    altitude_error = [abs(r["z"] - CRUISE_Z) for r in cruising]
    mean_altitude_error = sum(altitude_error) / len(altitude_error) if altitude_error else 0.0

    # Distance to the nearest corner: a tracking error that needs no leg schedule.
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
            if not force and out.exists() and out.stat().st_mtime >= odom.stat().st_mtime:
                skipped += 1
                continue
            rows = _read_odom(odom)
            if not rows:
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
