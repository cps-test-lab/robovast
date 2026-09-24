# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Per-run flight metrics for a PX4-flown trial: trajectory.csv (what the panels bind to) and
metrics.csv (one row of scalars).

Two position sources. The tracking metrics are computed against PX4's estimate
(/fmu/out/vehicle_local_position_v1, NED), because that is what the stack flew on and what a
field test records. Ground truth is the simulator's own sim_poses.csv (ENU), which every roqsim
run writes; this airframe has no controller and so no odometry topic. The difference between the
two is the estimator error. NED -> ENU is converted once, in _read_px4. Both sources are on the
simulated clock (the bag is recorded with use_sim_time), so neither is rebased.
"""

from __future__ import annotations

import csv
import math
from pathlib import Path
from typing import Optional, Tuple

from robovast.results_processing.postprocessing_plugins import \
    BasePostprocessingPlugin

# ENU. Must match scenario.osc.
CRUISE_Z = 3.0
COURSE = [(5.0, 5.0), (-5.0, 5.0), (-5.0, -5.0), (0.0, 0.0)]

# Climb settled .. landing commanded, in simulated seconds from the run start. Without both
# bounds the climb and the landing count as failure to hold altitude in every cell alike. EKF2
# convergence time varies, so this is a coarse cut, not a phase boundary.
CRUISE_START_S = 20.0
CRUISE_END_S = 80.0

GROUNDED_Z = 0.5
HOLD_FRACTION = 0.75
HOLD_TOLERANCE_M = 0.5
# Above this the tracking metrics no longer separate a control failure from an estimation one.
ESTIMATOR_DIVERGED_M = 1.0

# Above this a timestamp is a Unix epoch, not a simulated second.
WALL_CLOCK_SUSPECT_S = 3.0e7

# The airframe's MuJoCo body name in sim_poses.csv: spawn_robot prefix + model root body.
TRUTH_FRAME = "x500_x500"


def _quat_tilt_deg(x: float, y: float, z: float, w: float) -> float:
    """Angle between the body z axis and vertical: the third column of the rotation matrix."""
    body_z = 1.0 - 2.0 * (x * x + y * y)
    return math.degrees(math.acos(max(-1.0, min(1.0, body_z))))


def _read_px4(path: Path) -> list[dict]:
    """PX4's estimate as ENU samples on the simulated clock. Rows before EKF2 has a solution carry
    zeros with xy_valid/z_valid false and are dropped; a bag on wall time is refused."""
    rows = []
    with open(path, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            try:
                if row.get("xy_valid", "True") in ("False", "false", "0"):
                    continue
                if row.get("z_valid", "True") in ("False", "false", "0"):
                    continue
                north, east, down = float(row["x"]), float(row["y"]), float(row["z"])
                rows.append(
                    {
                        "t": float(row["timestamp"]) * 1e-9,
                        "x": east,
                        "y": north,
                        "z": -down,
                        "vx": float(row["vy"]),
                        "vy": float(row["vx"]),
                        "vz": -float(row["vz"]),
                    }
                )
            except (KeyError, ValueError, TypeError):
                continue
    if rows and rows[0]["t"] > WALL_CLOCK_SUSPECT_S:
        raise ValueError(
            f"{path}: the first estimate is stamped {rows[0]['t']:.0f} s, which is a wall-clock "
            f"epoch, not the simulated clock. The bag must be recorded with use_sim_time, or the "
            f"estimate can never be paired with the simulator's pose table."
        )
    return rows


def _read_sim_poses(path: Path, frame: str) -> list[dict]:
    """One body's rows of sim_poses.csv; [] when it has none."""
    rows = []
    with open(path, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row.get("frame") != frame:
                continue
            try:
                rows.append(
                    {
                        "t": float(row["timestamp"]),
                        "x": float(row["position.x"]),
                        "y": float(row["position.y"]),
                        "z": float(row["position.z"]),
                        "qx": float(row["orientation.x"]),
                        "qy": float(row["orientation.y"]),
                        "qz": float(row["orientation.z"]),
                        "qw": float(row["orientation.w"]),
                        "vx": float(row["twist.linear.x"]),
                        "vy": float(row["twist.linear.y"]),
                        "vz": float(row["twist.linear.z"]),
                    }
                )
            except (KeyError, ValueError, TypeError):
                continue
    return rows


def _pair(truth: list[dict], est: list[dict]) -> list[Optional[dict]]:
    """Nearest estimate per truth sample, by a cursor walk over both monotonic series. Not
    interpolated: interpolating smooths the estimate and flatters the error."""
    out: list[Optional[dict]] = []
    if not est:
        return [None] * len(truth)
    i = 0
    for row in truth:
        while i + 1 < len(est) and abs(est[i + 1]["t"] - row["t"]) <= abs(est[i]["t"] - row["t"]):
            i += 1
        out.append(est[i])
    return out


def _trajectory(truth: list[dict], est: list[dict]) -> list[dict]:
    out = []
    for row, e in zip(truth, _pair(truth, est)):
        est_error = (
            math.dist((row["x"], row["y"], row["z"]), (e["x"], e["y"], e["z"]))
            if e is not None else ""
        )
        out.append(
            {
                "t": round(row["t"], 4),
                "x": round(row["x"], 4),
                "y": round(row["y"], 4),
                "z": round(row["z"], 4),
                "est_x": round(e["x"], 4) if e is not None else "",
                "est_y": round(e["y"], 4) if e is not None else "",
                "est_z": round(e["z"], 4) if e is not None else "",
                "est_error": round(est_error, 4) if e is not None else "",
                "tilt_deg": round(_quat_tilt_deg(row["qx"], row["qy"], row["qz"], row["qw"]), 3),
                "speed": round(math.sqrt(row["vx"] ** 2 + row["vy"] ** 2 + row["vz"] ** 2), 4),
            }
        )
    return out


def _outcome(traj: list[dict], mean_altitude_error: float, mean_estimator_error: float) -> str:
    """never_armed: PX4 refused to arm or T/W <= 1 (the run log separates them). estimator_diverged
    is checked before sagged: a diverged estimate makes the tracking numbers meaningless."""
    if not traj or max(r["z"] for r in traj) < GROUNDED_Z:
        return "never_armed"
    if mean_estimator_error > ESTIMATOR_DIVERGED_M:
        return "estimator_diverged"
    if mean_altitude_error > HOLD_TOLERANCE_M:
        return "sagged"
    return "held"


def _metrics(traj: list[dict]) -> dict:
    duration = traj[-1]["t"] if traj else 0.0

    cruising = [r for r in traj if CRUISE_START_S <= r["t"] < CRUISE_END_S]
    if not cruising:
        cruising = traj

    held = [r for r in cruising if r["z"] >= HOLD_FRACTION * CRUISE_Z]
    cruise_hold = len(held) / len(cruising) if cruising else 0.0

    # Against the estimate where there is one; ground truth where the estimator never covered.
    def _flown_z(row):
        return row["est_z"] if row["est_z"] != "" else row["z"]

    def _flown_xy(row):
        if row["est_x"] != "":
            return (row["est_x"], row["est_y"])
        return (row["x"], row["y"])

    altitude_error = [abs(_flown_z(r) - CRUISE_Z) for r in cruising]
    mean_altitude_error = sum(altitude_error) / len(altitude_error) if altitude_error else 0.0

    # Distance to the nearest corner: a tracking error that needs no leg schedule.
    tracking = [min(math.dist(_flown_xy(r), corner) for corner in COURSE) for r in cruising]
    tracking_rmse = math.sqrt(sum(e * e for e in tracking) / len(tracking)) if tracking else 0.0

    est_errors = [r["est_error"] for r in cruising if r["est_error"] != ""]
    mean_estimator_error = sum(est_errors) / len(est_errors) if est_errors else 0.0
    max_estimator_error = max(est_errors) if est_errors else 0.0

    landed = traj[-1]
    return {
        "outcome": _outcome(traj, mean_altitude_error, mean_estimator_error),
        "cruise_hold_fraction": round(cruise_hold, 4),
        "mean_altitude_error": round(mean_altitude_error, 4),
        "tracking_rmse": round(tracking_rmse, 4),
        "mean_estimator_error": round(mean_estimator_error, 4),
        "max_estimator_error": round(max_estimator_error, 4),
        "max_tilt_deg": round(max(r["tilt_deg"] for r in traj), 3),
        "max_speed": round(max(r["speed"] for r in traj), 4),
        # From ground truth: where the aircraft physically came to rest.
        "landing_error": round(math.dist((landed["x"], landed["y"]), (0.0, 0.0)), 4),
        "final_z": round(landed["z"], 4),
        "duration_s": round(duration, 3),
    }


def _write_csv(path: Path, rows: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


class EnvelopeMetrics(BasePostprocessingPlugin):

    def __call__(self, results_dir: str, config_dir: str,
                 truth_glob: str = "sim_poses.csv",
                 truth_frame: str = TRUTH_FRAME,
                 px4_glob: str = "*vehicle_local_position*.csv",
                 trajectory: str = "trajectory.csv",
                 file: str = "metrics.csv", force: bool = False, **kwargs) -> Tuple[bool, str]:
        written = skipped = empty = no_estimate = 0
        for poses in sorted(Path(results_dir).rglob(truth_glob)):
            run_dir = poses.parent
            out = run_dir / file
            if not force and out.exists() and out.stat().st_mtime >= poses.stat().st_mtime:
                skipped += 1
                continue
            truth = _read_sim_poses(poses, truth_frame)
            if not truth:
                empty += 1
                continue

            px4_files = sorted(run_dir.glob(px4_glob))
            est = _read_px4(px4_files[0]) if px4_files else []
            if not est:
                # Still scored from ground truth; a non-zero count means PX4 was never in the loop.
                no_estimate += 1

            traj = _trajectory(truth, est)
            _write_csv(run_dir / trajectory, traj)
            _write_csv(out, [_metrics(traj)])
            written += 1

        notes = []
        if skipped:
            notes.append(f"{skipped} up-to-date")
        if empty:
            notes.append(f"{empty} with no pose rows for {truth_frame!r}")
        if no_estimate:
            notes.append(f"{no_estimate} with NO PX4 position estimate")
        suffix = f" ({', '.join(notes)})" if notes else ""
        return True, f"EnvelopeMetrics wrote {file} for {written} run(s){suffix}"
