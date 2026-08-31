# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Flight-envelope metrics for a PX4-flown trial, from two position sources rather than one.

Adapted from ``../../drone_flight_envelope/files/metrics.py``. The structure is the same -- per
run, read what ``rosbags_to_csv`` extracted and write ``trajectory.csv`` (what the run-view panels
bind to) and ``metrics.csv`` (one row of scalars) beside it -- and one thing about it is different
in a way worth understanding before copying either file.

**Two position sources, and the primary one is the estimate.**

``/fmu/out/vehicle_local_position_v1``
    Where PX4's EKF2 *believed* the aircraft was, in NED. This is the position PX4 actually flew
    on, so it is what the flight-tracking metrics are computed against. That is not a compromise
    forced by the setup -- it is what a real flight test measures. A flight test has no ground
    truth; it has an autopilot log, and the number that matters operationally is how well the
    vehicle held the course *it thought it was holding*. Measuring against simulator ground truth
    would answer a question no field test can ask.

``/drone/odom``
    Ground truth, from the roqsim bridge, in ENU. Kept because the simulator can answer the
    question the field cannot, and the difference between the two is itself an observable:
    ``mean_estimator_error`` is how far EKF2's belief drifted from reality. In the Crazyflie
    example there was no such quantity to have -- the controller read ground truth directly, so
    belief and truth were the same object.

**NED vs ENU is reconciled exactly once, here.** PX4 is [north, east, DOWN]; roqsim is [east,
north, up]. The conversion is
    x_enu = y_ned      y_enu = x_ned      z_enu = -z_ned
and it is applied to the PX4 samples on the way in, so every column downstream -- ``trajectory.csv``,
every panel, every metric -- is ENU with altitude positive up. Doing it in one place is the point:
the scenario states its course in NED and the world states its markers in ENU, and this is the
only file that has to hold both conventions in mind at once.

Used as a local-file postprocessing plugin (``files/metrics.py:EnvelopeMetrics``) under
``results_processing.postprocessing``. Run discovery globs, so it works pointed at a campaign root
or a single run.
"""

from __future__ import annotations

import csv
import math
from pathlib import Path
from typing import Optional, Tuple

from robovast.results_processing.postprocessing_plugins import \
    BasePostprocessingPlugin

#: Commanded cruise altitude and course, in ENU metres -- the scenario's NED legs with the sign
#: convention undone. Restated here rather than parsed out of the .osc because a metric needs to
#: know what "on target" meant; if the scenario's course changes, this changes with it.
CRUISE_Z = 3.0
COURSE = [(5.0, 5.0), (-5.0, 5.0), (-5.0, -5.0), (0.0, 0.0)]

#: The cruise window, in seconds from the first sample: after the climb has settled and **before**
#: the landing is commanded. Both ends matter. Without the lower bound the climb counts as failure
#: to hold altitude; without the upper bound the commanded landing does, and since every run lands,
#: the hold fraction then reads the same in every cell no matter what was varied -- a metric that
#: looks like a measurement and discriminates nothing.
#:
#: The window is later and longer than the Crazyflie example's because PX4 has to converge EKF2 and
#: arm before anything flies: the bag starts at bringup, not at takeoff.
CRUISE_START_S = 20.0
CRUISE_END_S = 80.0

#: Altitude below which the aircraft is on the ground rather than flying. Larger than the Crazyflie
#: example's 0.15 m because the airframe is 0.5 m across and its origin sits well above the floor.
GROUNDED_Z = 0.5
#: Fraction of the commanded cruise altitude that still counts as holding it, for the
#: `cruise_hold_fraction` diagnostic.
HOLD_FRACTION = 0.75
#: Mean altitude error, in metres, up to which the aircraft counts as having held station. 0.5 m at
#: a 3 m cruise is the same relative band the Crazyflie example used at 1.2 m -- generous, but one
#: you can see it leave.
HOLD_TOLERANCE_M = 0.5
#: Mean |truth - estimate|, in metres, above which the flight is called out as an estimator
#: problem rather than a control one. Chosen as the point where the divergence is larger than the
#: hold tolerance itself: past there, PX4 could be flying a perfect course against a wrong belief
#: and the two failure modes are no longer separable by the tracking metrics.
ESTIMATOR_DIVERGED_M = 1.0


def _quat_tilt_deg(x: float, y: float, z: float, w: float) -> float:
    """Angle between the body z axis and vertical, in degrees.

    From the quaternion rather than from roll/pitch: Euler angles are lossy the moment a body
    leaves the plane, and a quadrotor rejecting a gust is exactly that case. The body z axis in
    world coordinates is the third column of the rotation matrix, so the tilt is its angle to
    (0, 0, 1).
    """
    body_z = 1.0 - 2.0 * (x * x + y * y)
    return math.degrees(math.acos(max(-1.0, min(1.0, body_z))))


def _rebase(rows: list[dict]) -> list[dict]:
    if not rows:
        return []
    t0 = rows[0]["t"]
    for row in rows:
        row["t"] -= t0
    return rows


def _read_px4(path: Path) -> list[dict]:
    """VehicleLocalPosition as ENU samples: PX4's belief, converted once and here only.

    ``xy_valid`` / ``z_valid`` are honoured rather than ignored. Before EKF2 has a position
    solution the message is still published with zeros in the position fields, and taking those at
    face value puts the aircraft at the origin for the first seconds of every run -- which reads as
    a drone that sat on the pad, i.e. exactly the failure this campaign is trying to detect.
    """
    rows = []
    with open(path, newline="") as handle:
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
                        # NED -> ENU. The only place in this example where this happens.
                        "x": east,
                        "y": north,
                        "z": -down,
                        "vx": float(row["vy"]),
                        "vy": float(row["vx"]),
                        "vz": -float(row["vz"]),
                    }
                )
            except (KeyError, ValueError, TypeError):
                # A malformed row is skipped rather than failing the run: one bad sample must not
                # discard an otherwise complete flight.
                continue
    return _rebase(rows)


def _read_odom(path: Path) -> list[dict]:
    """Ground truth from the roqsim bridge, already ENU. Returns [] when the bag had no messages."""
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
    return _rebase(rows)


def _pair(truth: list[dict], est: list[dict]) -> list[Optional[dict]]:
    """For each ground-truth sample, the nearest PX4 estimate (or None when there is none).

    A cursor walk rather than a search per sample -- both series are monotonic in t, so this is
    linear where the obvious nested scan is quadratic over a full flight. Nearest-neighbour rather
    than interpolating, deliberately: the two streams run at different rates, and interpolating a
    position smooths it, which would show up as *reduced* estimator error -- i.e. it would flatter
    the very quantity being measured.
    """
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
    """One row per ground-truth sample, carrying both positions and their difference.

    Keyed on ground truth because that is the series the 3D scene and the ground track are drawn
    from; the estimate rides alongside it so the run view can show belief and reality on one axis.
    """
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
    """One label for the whole flight, in the vocabulary the envelope is drawn in.

    Four outcomes rather than pass/fail, because the interesting part of this campaign is *how* a
    configuration fails -- and with a real flight stack in the loop there is one more way to fail
    than there was with a PD loop reading ground truth:

    ``never_armed``
        No flight at all. Either PX4 refused to arm (EKF2 never got a position solution, a preflight
        check failed) or the aircraft could not lift itself -- T/W at or below 1. Both are "the
        stack declined to fly", and the run log separates them; the metrics cannot.
    ``estimator_diverged``
        Airborne, but EKF2's belief drifted away from reality. Checked FIRST, because a diverged
        estimate makes every tracking number below it meaningless: PX4 may have flown its
        commanded course perfectly against a position that was wrong. This outcome does not exist
        in the Crazyflie example and cannot -- there was no estimator to diverge.
    ``sagged``
        Airborne and correctly estimated, but below the altitude it was told to hold. Out of
        thrust margin: PX4's position controller is commanding more than the rotors can deliver.
    ``held``
        Flew the course within tolerance of the commanded altitude.
    """
    if not traj or max(r["z"] for r in traj) < GROUNDED_Z:
        return "never_armed"
    if mean_estimator_error > ESTIMATOR_DIVERGED_M:
        return "estimator_diverged"
    if mean_altitude_error > HOLD_TOLERANCE_M:
        return "sagged"
    return "held"


def _metrics(traj: list[dict]) -> dict:
    duration = traj[-1]["t"] if traj else 0.0

    # Cruise phase: the commanded course, with bringup, the climb and the landing excluded, so that
    # an aircraft which never climbs is measured over the same window as one that does.
    cruising = [r for r in traj if CRUISE_START_S <= r["t"] < CRUISE_END_S]
    if not cruising:
        cruising = traj

    held = [r for r in cruising if r["z"] >= HOLD_FRACTION * CRUISE_Z]
    cruise_hold = len(held) / len(cruising) if cruising else 0.0

    # The flight-tracking metrics are computed against PX4's ESTIMATE where there is one -- what the
    # stack believed and acted on, which is what a real flight test records -- and fall back to
    # ground truth only for samples the estimator never covered. See the module docstring.
    def _flown_z(row):
        return row["est_z"] if row["est_z"] != "" else row["z"]

    def _flown_xy(row):
        if row["est_x"] != "":
            return (row["est_x"], row["est_y"])
        return (row["x"], row["y"])

    altitude_error = [abs(_flown_z(r) - CRUISE_Z) for r in cruising]
    mean_altitude_error = sum(altitude_error) / len(altitude_error) if altitude_error else 0.0

    # Distance from the nearest commanded corner, as a crude tracking error that does not need the
    # leg schedule: the course is a square, so the nearest corner is the one being flown to or from.
    tracking = [min(math.dist(_flown_xy(r), corner) for corner in COURSE) for r in cruising]
    tracking_rmse = math.sqrt(sum(e * e for e in tracking) / len(tracking)) if tracking else 0.0

    # The new one. Over the cruise window only: before arming EKF2 has no solution worth scoring,
    # and after touchdown the aircraft is not moving, so both ends would dilute it towards zero.
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
        # Landing accuracy from GROUND TRUTH and not from the estimate, deliberately: where the
        # aircraft physically came to rest is a fact about the flight, and scoring it against a
        # belief that may have drifted would credit a good landing to a bad estimate.
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
                 odom_glob: str = "*drone_odom.csv",
                 px4_glob: str = "*vehicle_local_position*.csv",
                 trajectory: str = "trajectory.csv",
                 file: str = "metrics.csv", force: bool = False, **kwargs) -> Tuple[bool, str]:
        written = skipped = empty = no_estimate = 0
        for odom in sorted(Path(results_dir).rglob(odom_glob)):
            run_dir = odom.parent
            out = run_dir / file
            # Incremental: a re-run over the whole campaign only touches new runs. `force` redoes all.
            if not force and out.exists() and out.stat().st_mtime >= odom.stat().st_mtime:
                skipped += 1
                continue
            truth = _read_odom(odom)
            if not truth:
                # A run that produced no odometry is reported, not silently skipped: it means the
                # simulator or the bridge never came up, which is a different problem from a drone
                # that flew badly.
                empty += 1
                continue

            px4_files = sorted(run_dir.glob(px4_glob))
            est = _read_px4(px4_files[0]) if px4_files else []
            if not est:
                # Counted and reported rather than passed over. No PX4 position means the flight
                # stack was never in the loop -- the uXRCE-DDS bridge did not come up, or the topic
                # is named differently in this PX4 release (see scenario.osc). The run is still
                # scored from ground truth so the trajectory is not lost, but a campaign where this
                # count is non-zero is not a campaign about PX4.
                no_estimate += 1

            traj = _trajectory(truth, est)
            _write_csv(run_dir / trajectory, traj)
            _write_csv(out, [_metrics(traj)])
            written += 1

        notes = []
        if skipped:
            notes.append(f"{skipped} up-to-date")
        if empty:
            notes.append(f"{empty} with no odometry")
        if no_estimate:
            notes.append(f"{no_estimate} with NO PX4 position estimate")
        suffix = f" ({', '.join(notes)})" if notes else ""
        return True, f"EnvelopeMetrics wrote {file} for {written} run(s){suffix}"
