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

"""``ground_truth_poses``: where the robot truly was, one table whichever simulator ran the run.

Ground truth has a producer per simulator. A simulator that records its own state has every
body's world pose from inside the simulation; one that does not publishes the true pose on
``/tf`` as a leaf frame named ``<robot>_base_link_gt``, beside the localization tree. This
table reads the one the campaign names and gives both the same shape, so an analysis reads
ground truth without knowing which simulator produced it.

Where it comes from is the ``ground_truth`` entry of the decoder's configuration, written from
the simulator backend of the campaign (``SimulatorBackend.ground_truth``):

``{table: <pose table>, frame_suffix: <suffix>}``
    the frames of that table whose name ends in *suffix*; the default, :data:`TF_SOURCE`,
    for a campaign whose backend names none.
``{table: <pose table>, entity_kind: <kind>}``
    the bodies the run's ``sim_entities`` roster lists for entities of *kind*.

Rows are in the run's world frame, one per frame per sample. ``timestamp`` is the time the pose
was true, in sim seconds: the source's ``stamp`` where it has one (a pose that arrived over a
transport), else its ``timestamp`` (a pose the simulator wrote itself). A run whose source has no
such frame gets no rows and a reason, never an empty table that reads as a robot that did not
move.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

import pyarrow as pa
import pyarrow.compute as pc

from .authored import with_yaw
from .tables import read_run_table

GROUND_TRUTH = "ground_truth_poses"

#: The ROS convention: the true world pose on ``/tf`` as the leaf frame ``<robot>_base_link_gt``.
TF_SOURCE = {"table": "poses", "frame_suffix": "_gt"}

#: The roster an ``entity_kind`` source reads.
ENTITIES_TABLE = "sim_entities"

#: The columns after the run's context, in order; ``orientation.yaw`` is derived after them.
COLUMNS = ["timestamp", "frame", "source_table",
           "position.x", "position.y", "position.z",
           "orientation.x", "orientation.y", "orientation.z", "orientation.w",
           "twist.linear.x", "twist.linear.y", "twist.linear.z",
           "twist.angular.x", "twist.angular.y", "twist.angular.z"]

_TEXT = {"frame", "source_table"}

#: Column notes, for the catalog.
NOTES = {
    "timestamp": (
        "SIM seconds at which the pose was true: the simulator's own sample time, or the "
        "stamp of the ground-truth transform. Safe to difference."),
    "frame": (
        "the entity in its producer's vocabulary: a simulator body, or the ground-truth TF "
        "frame (<robot>_base_link_gt). One robot is one frame."),
    "source_table": (
        "the table these rows were taken from: which producer answered for this run."),
    "twist.linear.x": "world-frame velocity where the producer knows it; NULL from /tf.",
}


def source_of(config: Optional[dict]) -> dict:
    """The ground-truth source the decoder configuration names, or :data:`TF_SOURCE`."""
    source = (config or {}).get("ground_truth")
    if source is None:
        return dict(TF_SOURCE)
    if not isinstance(source, dict) or not isinstance(source.get("table"), str):
        raise ValueError(f"ground_truth: expected a mapping with 'table', got {source!r}")
    selector = set(source) - {"table"}
    if selector not in ({"frame_suffix"}, {"entity_kind"}):
        raise ValueError(f"ground_truth: expected 'table' and one of 'frame_suffix' or "
                         f"'entity_kind', got {sorted(source)}")
    return dict(source)


def inputs(source: dict) -> List[str]:
    """The tables *source* reads, built before this one."""
    return [source["table"]] + ([ENTITIES_TABLE] if "entity_kind" in source else [])


def _reason_of(manifest: dict, table: str, run_key: str) -> str:
    entry = manifest.get("tables", {}).get(table, {}).get("runs", {}).get(run_key) or {}
    return f": {entry['reason']}" if entry.get("reason") else ""


def _frames(campaign_dir: str, manifest: dict, run_key: str, source: dict,
            present: List[str]) -> Tuple[List[str], Optional[str]]:
    """The frames of the source that are the ground truth, or ``([], reason)``."""
    table = source["table"]
    if "frame_suffix" in source:
        suffix = source["frame_suffix"]
        frames = [f for f in present if f.endswith(suffix)]
        if not frames:
            return [], (f"no frame of {table} ends in '{suffix}' (it has: "
                        f"{', '.join(present) or 'none'}); the ground-truth publisher's frame "
                        f"must be recorded and extracted (rosbags_tf_to_csv frames)")
        return frames, None
    kind = source["entity_kind"]
    roster = read_run_table(campaign_dir, manifest, ENTITIES_TABLE, run_key)
    if roster is None:
        return [], (f"{ENTITIES_TABLE} has no rows for this run"
                    f"{_reason_of(manifest, ENTITIES_TABLE, run_key)}")
    bodies = list(dict.fromkeys(e["body"] for e in roster.to_pylist()
                                if e.get("kind") == kind and e.get("body")))
    if not bodies:
        return [], f"{ENTITIES_TABLE} lists no entity of kind '{kind}'"
    frames = [b for b in bodies if b in present]
    if not frames:
        return [], (f"{table} has no rows for the {kind} bodies {', '.join(bodies)}: is the "
                    f"body left out of the simulator's recording?")
    return frames, None


def derive(campaign_dir: str, manifest: dict, run_key: str, source: dict,
           context: dict) -> Tuple[Optional[pa.Table], Optional[str]]:
    """The run's ground-truth rows from its *source* table, or ``(None, reason)``."""
    table = source["table"]
    rows = read_run_table(campaign_dir, manifest, table, run_key)
    if rows is None or rows.num_rows == 0:
        return None, (f"{table} has no rows for this run"
                      f"{_reason_of(manifest, table, run_key)}")
    # pyarrow.compute's functions are generated at import, which pylint cannot see.
    # pylint: disable=no-member
    present = sorted(f for f in pc.unique(rows.column("frame")).to_pylist() if f)
    frames, reason = _frames(campaign_dir, manifest, run_key, source, present)
    if reason:
        return None, reason
    clock = "stamp" if "stamp" in rows.column_names else "timestamp"
    selected = rows.filter(pc.and_(pc.is_in(rows.column("frame"),
                                            value_set=pa.array(frames, type=pa.string())),
                                   pc.is_valid(rows.column(clock))))
    # pylint: enable=no-member
    selected = selected.sort_by([("frame", "ascending"), (clock, "ascending")])
    n = selected.num_rows
    arrays = {key: pa.array([value] * n, type=pa.int64() if key == "run_id" else pa.string())
              for key, value in context.items()}
    for column in COLUMNS:
        kind = pa.string() if column in _TEXT else pa.float64()
        if column == "timestamp":
            arrays[column] = selected.column(clock).cast(pa.float64())
        elif column == "source_table":
            arrays[column] = pa.array([table] * n, type=kind)
        elif column in selected.column_names:
            arrays[column] = selected.column(column).cast(kind)
        else:
            arrays[column] = pa.nulls(n, type=kind)
    return with_yaw(pa.table(arrays)), None


__all__ = ["COLUMNS", "ENTITIES_TABLE", "GROUND_TRUTH", "NOTES", "TF_SOURCE", "derive",
           "inputs", "source_of"]
