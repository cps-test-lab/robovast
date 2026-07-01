#!/usr/bin/env python3
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

"""Toy 2D quadrotor hover-and-land simulation (single file, numpy only).

A point quadrotor in the vertical plane (state: x, z, vx, vz, tilt theta) starts
offset and high, then descends to land on a pad at x=0 while horizontal wind with
random gusts pushes it. It is deliberately simple but **stochastic** (gusts +
sensor noise), so a given parameter set can land on one run and fail on another.

Three failure modes, each coupled to two parameters so a search must explore the
*joint* space to find them:

* ``hard_crash`` — touchdown speed too high. Max thrust is an absolute force, so
  the achievable deceleration ``T_MAX/mass - g`` shrinks with mass: heavy craft
  + high descent_rate cannot arrest in time. (mass x descent_rate)
* ``tip_over``   — tilt exceeds a limit. High wind pushes the craft, and a high
  thrust_gain leans aggressively and overshoots the tilt limit. (wind x gain)
* ``drift_miss`` — lands/ends off the pad. High wind with a low thrust_gain gives
  too little lean authority to counter the drift. (wind x gain)

Writes into the output directory:
* ``trajectory.csv`` — t,x,z,tilt time series (for visualization).
* ``metrics.csv``    — one labelled row of summary metrics (objective/descriptor
  source for future search strategies).

Exits non-zero on any failure so scenario_execution records a test failure.
"""

import argparse
import csv
import sys
from pathlib import Path

import numpy as np

G = 9.81
T_MAX = 32.0          # absolute max thrust [N] (does NOT scale with mass)
DT_DEFAULT = 0.02
Z0 = 5.0              # start altitude [m]
X0 = 0.0             # start above the pad; horizontal motion is wind-driven
FLARE_ALT = 1.5      # below this altitude, command a gentle touchdown
CRASH_SPEED = 1.0    # touchdown |vz| above this -> hard_crash [m/s]
TILT_LIMIT = 0.7     # |theta| above this -> tip_over [rad]
KVZ = 6.0            # vertical velocity gain (fixed; not the searched gain)
KX, KVX = 0.5, 1.0   # horizontal position / velocity gains
TILT_CMD_MAX = 1.2   # commanded tilt clamp [rad] (above TILT_LIMIT on purpose)
TILT_RATE = 3.0      # max tilt slew [rad/s]


def simulate(thrust_gain, mass, wind_strength, descent_rate, dt, horizon, pad_radius, rng):
    x, z, vx, vz, theta = X0, Z0, 0.0, 0.0, 0.0
    traj = [(0.0, x, z, vx, vz, theta)]
    max_tilt = 0.0
    effort = 0.0
    t = 0.0
    n_steps = int(horizon / dt)

    for _ in range(n_steps):
        # Noisy measurements (sensor noise) feed the controller.
        x_m = x + rng.normal(0, 0.02)
        vx_m = vx + rng.normal(0, 0.02)

        # Horizontal: lean to drive x->0. Authority scales with thrust_gain.
        theta_cmd = np.clip(-thrust_gain * (KX * x_m + KVX * vx_m),
                            -TILT_CMD_MAX, TILT_CMD_MAX)
        theta += np.clip(theta_cmd - theta, -TILT_RATE * dt, TILT_RATE * dt)

        # Vertical: track a descent profile, flaring near the ground. Uses a
        # fixed strong gain and saturates at T_MAX, so arresting is limited by
        # T_MAX/mass (mass coupling), not by thrust_gain.
        vz_des = -descent_rate if z > FLARE_ALT else -max(0.2, descent_rate * z / FLARE_ALT)
        thrust = mass * G / max(np.cos(theta), 0.5) + KVZ * mass * (vz_des - vz)
        thrust = float(np.clip(thrust, 0.0, T_MAX))

        # Wind: horizontal acceleration with random gusts (the stochastic part).
        a_wind = wind_strength * (1.0 + rng.normal(0, 0.3))
        ax = thrust / mass * np.sin(theta) + a_wind
        az = thrust / mass * np.cos(theta) - G

        vx += ax * dt
        vz += az * dt
        x += vx * dt
        z += vz * dt
        t += dt

        max_tilt = max(max_tilt, abs(theta))
        effort += abs(theta) * dt
        traj.append((t, x, z, vx, vz, theta))

        if abs(theta) > TILT_LIMIT:
            return _result("tip_over", t, x, vz, max_tilt, effort, traj, pad_radius)
        if z <= 0.0:
            return _result(_touchdown_outcome(vz, x, pad_radius),
                           t, x, vz, max_tilt, effort, traj, pad_radius)

    # Never reached the ground within the horizon.
    return _result("timeout", t, x, vz, max_tilt, effort, traj, pad_radius)


def _touchdown_outcome(vz, x, pad_radius):
    if abs(vz) > CRASH_SPEED:
        return "hard_crash"
    if abs(x) > pad_radius:
        return "drift_miss"
    return "landed"


def _result(outcome, t, x, vz, max_tilt, effort, traj, pad_radius):
    return {
        "outcome": outcome,
        "success": outcome == "landed",
        "land_time": round(t, 3),
        "landing_speed": round(abs(vz), 4),
        "drift_dist": round(abs(x), 4),
        "max_tilt": round(max_tilt, 4),
        "control_effort": round(effort, 4),
        "trajectory": traj,
    }


def _write_csv(path, header, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description="Toy 2D quadrotor hover-and-land simulation")
    parser.add_argument("--thrust-gain", type=float, required=True, help="Horizontal control aggressiveness")
    parser.add_argument("--mass", type=float, required=True, help="Vehicle mass [kg]")
    parser.add_argument("--wind-strength", type=float, required=True, help="Mean horizontal wind accel [m/s^2]")
    parser.add_argument("--descent-rate", type=float, required=True, help="Commanded descent speed [m/s]")
    parser.add_argument("--dt", type=float, default=DT_DEFAULT, help="Integration step [s]")
    parser.add_argument("--horizon", type=float, default=40.0, help="Max simulated time [s]")
    parser.add_argument("--pad-radius", type=float, default=0.5, help="Landing pad radius [m]")
    parser.add_argument("--seed", type=int, default=None, help="RNG seed (default: nondeterministic)")
    parser.add_argument("--output", type=str, default=".", help="Output directory")
    args = parser.parse_args()

    rng = np.random.default_rng(args.seed)
    result = simulate(args.thrust_gain, args.mass, args.wind_strength, args.descent_rate,
                      args.dt, args.horizon, args.pad_radius, rng)

    # The sim emits only RAW data: the trajectory (incl. velocities). Behavior
    # measures + objective are derived afterward by the search extract module
    # (configs/examples/quadrotor_landing/search/extract.py), so there is no
    # sim-written metrics.csv. Pass/fail is still signalled via the exit code.
    out_dir = Path(args.output)
    _write_csv(out_dir / "trajectory.csv", ["t", "x", "z", "vx", "vz", "tilt"],
               result["trajectory"])

    print(f"Outcome: {result['outcome']} | land_time={result['land_time']}s "
          f"landing_speed={result['landing_speed']} drift={result['drift_dist']} "
          f"max_tilt={result['max_tilt']}")

    if not result["success"]:
        print(f"FAILURE: {result['outcome']}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
