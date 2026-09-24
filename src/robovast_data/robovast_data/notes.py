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

"""What a column means, where its name does not say: the notes a catalog shows beside it.

A note belongs to a column's meaning, which does not change between campaigns, so notes are
code rather than data. Which notes a table earns is decided from its columns:

* a pose-contract table (:func:`robovast_data.views.pose_clock`) gets the notes of its clock
  shape -- a table converted from a transport has an arrival ``timestamp`` that must not be
  differenced and a ``stamp`` that must, while a table the simulator wrote (``sim_poses``,
  decoded from roqsim's own recording) has one exact clock and no ``stamp`` -- and the
  orientation note;
* ``runs`` and the derived tables get the notes their builders declare
  (:data:`robovast_decode.runs.NOTES`, :data:`robovast_decode.derived.NOTES`);
* a few columns are noted by name (:data:`STATIC_NOTES`).
"""

from __future__ import annotations

from typing import Dict, Iterable

from robovast_decode.derived import NOTES as DERIVED_NOTES
from robovast_decode.runs import NOTES as RUNS_NOTES
from robovast_decode.runs import RUNS_TABLE

from .views import pose_clock

#: Split by clock shape, because the contract has two and the advice inverts between them.
_POSE_TRANSPORT_CLOCK_NOTES = {
    "timestamp": (
        "ARRIVAL time, and the join key every other table in this campaign shares -- use it "
        "to read poses against costmaps, behaviors and run_log, and to place a row on the "
        "run view's timeline. Do NOT difference it: it is quantized to the simulator's "
        "/clock grid and jittered by delivery, so a speed derived from it measures the "
        "transport rather than the robot. Use `stamp` for that."),
    "stamp": (
        "MEASUREMENT time -- when the pose was actually true, from the publisher's own "
        "header. This is the correct base for any derivative (speed, rate, dt); sort by it "
        "too, since ordering by `timestamp` leaves rows within one arrival tick in arbitrary "
        "order. NULL where the producer could not state one (a latched /tf_static "
        "transform)."),
}

#: The same two columns for a pose table the SIMULATOR wrote: no transport sat between the pose
#: and the row, so there is nothing to correct for and no `stamp` to point at.
_POSE_NATIVE_CLOCK_NOTES = {
    "timestamp": (
        "SIMULATED seconds, taken inside the simulator at the moment the pose was true -- this "
        "is the exact quantity the `poses` table's `stamp` is, not the arrival time that "
        "table's `timestamp` is. Difference it freely. Better still, do not: twist.linear.* "
        "and twist.angular.* are the true velocities, with no interval to get wrong."),
    "wall_time": (
        "Unix epoch seconds for the same sample, so a row can be placed against anything "
        "stamped in wall time -- run_log, resource_usage, a container's own log. It is the "
        "only bridge those have to this table on a run with no rosbag. Do NOT difference it "
        "or join poses on it: it advances with the host, which under a simulator that does "
        "not run in real time is not the run's clock. Use `timestamp` for both."),
}

#: Attached wherever a quaternion was ingested, which is every pose table regardless of clock.
_POSE_ORIENTATION_NOTES = {
    "orientation.yaw": (
        "DERIVED when the table is built, from orientation.x/y/z/w, and a planar projection: "
        "correct for a body in the plane, insufficient for one that pitches or rolls (a "
        "drone, a tilting arm, a robot on a ramp). The quaternion is what the producer "
        "emitted -- read that when the body is not flat."),
}

#: Notes keyed on ``(table, column)``, for columns whose name gives no hint of how they must
#: be read.
STATIC_NOTES: dict = {
    ("resource_usage", "cpu_percent"): (
        "one row is one PROCESS NAME, not a container: SUM per (container, wall_ts) before "
        "comparing, or an average reads as a per-process figure. Per-core, so >100 is "
        "normal -- full saturation is 100 * runs.available_cpus, which is the denominator to "
        "normalise by before comparing runs on different hosts."),
    ("resource_usage", "memory_rss_bytes"): (
        "summed RSS, so pages shared between a process and its forks are counted more than "
        "once. An upper bound -- read it as a trend, not as an absolute footprint."),
    ("joint_states", "timestamp"): (
        "SIMULATED seconds, taken inside the simulator at the sample: the exact clock, safe "
        "to difference for a joint velocity. One row per joint per sample, so pivot on "
        "`joint` before differencing."),
    ("joint_states", "wall_time"): (
        "Unix epoch seconds of the same sample, the bridge to run_log and resource_usage on "
        "a run with no rosbag. Not the run's clock: use `timestamp`."),
    ("joint_states", "position"): (
        "the joint's generalised coordinate: radians for a hinge, meters for a slide. Which "
        "it is follows from the joint's name and the model (sim_recording.model_json)."),
    ("sim_recording", "capture_fps"): (
        "the recording's sample rate as exact `num/den` text, snapped to a whole number of "
        "physics steps: 1/(rate) is the interval between consecutive sim_poses samples."),
    ("sim_recording", "timestep"): (
        "the physics step in simulated seconds; capture_fps is a multiple of it."),
    ("sim_recording", "overrides_json"): (
        "JSON: the world overrides this run was started with, the varied factors among "
        "them. Parse with json_extract; the run's param_ columns in `runs` are the same "
        "facts as the campaign declared them."),
    ("sim_recording", "packages_json"): (
        "JSON {package: version} of the simulator and its plugins as installed in the "
        "run's image: the provenance of the physics, beside the campaign's own manifest."),
    ("sim_recording", "model_json"): (
        "JSON: the compiled model's summary (body, joint and actuator counts and names). "
        "One row per run, so a query over the campaign compares what each run simulated."),
    ("sim_entities", "present"): (
        "1 while the entity is in the scene at the end of the run, 0 once despawned. The "
        "roster is the LAST one the recording carries: an entity spawned and removed during "
        "the run is a row with present 0, not a missing row."),
    ("sim_entities", "body"): (
        "the MuJoCo body the entity is rooted at: the `frame` its poses are filed under in "
        "sim_poses, which is how the two tables join."),
}



def notes_for(table: str, columns: Iterable[str]) -> Dict[str, str]:
    """``{column: note}`` for the columns of *table* that have one."""
    columns = set(columns)
    out = {column: note for (noted, column), note in STATIC_NOTES.items()
           if noted == table and column in columns}
    if table == RUNS_TABLE:
        out.update({c: n for c, n in RUNS_NOTES.items() if c in columns})
    out.update({c: n for c, n in DERIVED_NOTES.get(table, {}).items() if c in columns})
    clock = pose_clock(columns)
    if clock is not None:
        clock_notes = (_POSE_TRANSPORT_CLOCK_NOTES if clock == "stamp"
                       else _POSE_NATIVE_CLOCK_NOTES)
        out.update({c: n for c, n in {**clock_notes, **_POSE_ORIENTATION_NOTES}.items()
                    if c in columns})
    return out


__all__ = ["STATIC_NOTES", "notes_for"]
