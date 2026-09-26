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

"""Which tables a recording can give, and which handler fills each.

**Every recorded topic is a table unless there is a reason it is not.** A campaign that says
nothing gets:

* ``/tf`` + ``/tf_static`` -> ``poses`` (every frame that resolves against ``map``);
* ``/behavior_tree_log`` -> ``nav2_behavior_tree``;
* every ``nav_msgs/msg/OccupancyGrid`` topic -> ``costmaps``;
* every action's ``/<name>/_action/feedback`` and ``status`` -> ``action_<name>_feedback`` and
  ``action_<name>_status``;
* every other topic -> ``<bag>_<topic>``, one row per message;
* in the wall-time infrastructure recording, ``/rosout`` -> ``rosout`` and ``/clock`` ->
  ``clock_map``;
* in roqsim's own recording (:data:`ROQSIM_BAG`), ``poses`` -> ``sim_poses``, ``joints`` ->
  ``joint_states``, ``clock`` -> ``clock_map``, and its metadata -> ``sim_recording`` and
  ``sim_entities``.

What is recorded and *not* tabulated is said, per topic, with the reason
(:data:`BULK_TYPES`, :data:`NOT_TABULATED`, :data:`ROQSIM_NOT_TABULATED`): a camera image or a
point cloud is read from the recording itself, never copied into rows, and roqsim's raw
``state`` is read by roqsim. A laser scan is rows: its ranges are one list cell per message.

**A campaign's own configuration refines the defaults**, in the shape the ``.vast`` has always
written it -- ``{"groups": [{"bag_dir": ..., "plugins": [{"type": ..., ...}]}]}``: a configured
handler replaces the default for its topics (``tf_to_csv`` with ``require`` asserts the frames
an analysis depends on), and the rest of the recording keeps its defaults.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

from .handlers import (LEVEL_BY_NAME, ActionTopics, Clock, Costmaps, Handler, Nav2BtLog, Rosout,
                       SimClock, SimEntities, SimJoints, SimPoses, SimRecording, TfPoses,
                       TopicTable, Videos)
from .values import DEFAULT_CLOCK_TOLERANCE_S

#: The scenario recording, below a run, and the infrastructure recording, below a job.
SCENARIO_BAG = "rosbag2"
INFRA_BAG = "logs/rosout_bag"
#: roqsim's own recording, below a run: one mcap the simulator writes, beside or instead of
#: the scenario recording.
ROQSIM_BAG = "roqsim_bag"

#: Types whose payload is bulk: read from the recording where they are wanted, never rows. A
#: laser scan is not among them: its ranges are a numeric array like a covariance, one list
#: cell per message.
BULK_TYPES = frozenset({
    "sensor_msgs/msg/Image", "sensor_msgs/msg/CompressedImage", "sensor_msgs/msg/PointCloud2",
    "sensor_msgs/msg/PointCloud",
})

#: Topics of the scenario recording that other tables already cover, or that are noise.
NOT_TABULATED = {
    "/clock": "the run's clock map comes from the wall-time infrastructure recording",
    "/rosout": "log lines come from the infrastructure recording and the containers' output",
    "/parameter_events": "parameter changes are not a measurement",
}

#: Channels of roqsim's recording that are not tables.
ROQSIM_NOT_TABULATED = {
    "state": "raw simulator state, read by roqsim itself",
}

#: The reason a channel that would fill a table another recording of the run already gives
#: is not tabulated a second time.
_TAKEN = "the run's {table} table comes from its {role} recording"

_ACTION_TOPIC = re.compile(r"^/(?P<name>.+)/_action/(?P<kind>feedback|status)$")


@dataclass
class Plan:
    """The handlers for one recording, and what it will not tabulate."""
    handlers: List[Handler] = field(default_factory=list)
    #: ``{table: handler}`` for every table this recording can give.
    tables: Dict[str, Handler] = field(default_factory=dict)
    #: ``{topic: reason}`` for every recorded topic that is not a table.
    untabulated: Dict[str, str] = field(default_factory=dict)
    #: ``{topic: table}`` for every recorded topic that is one.
    topic_table: Dict[str, str] = field(default_factory=dict)


def _configured(plugins: Iterable[dict]) -> List[Handler]:
    """Handlers from the ``.vast``'s own entries for one recording."""
    out = []
    videos = []
    for cfg in plugins:
        kind = cfg.get("type", "")
        if kind == "tf_to_csv":
            out.append(TfPoses(frames=cfg.get("frames"), require=cfg.get("require"),
                               table=_stem(cfg.get("csv_filename"), TfPoses.TABLE)))
        elif kind == "nav2_bt_to_csv":
            out.append(Nav2BtLog())
        elif kind == "costmap_to_csv":
            out.append(Costmaps(cfg.get("topics") or []))
        elif kind == "to_csv":
            out.append(TopicTable(cfg.get("topics") or []))
        elif kind == "action_to_csv":
            out.append(ActionTopics(cfg["action"], prefix=cfg.get("filename_prefix")))
        elif kind == "rosout_to_csv":
            out.append(Rosout(LEVEL_BY_NAME.get(cfg.get("min_level", "DEBUG"), 10)))
        elif kind == "clock_to_csv":
            out.append(Clock(float(cfg.get("tolerance_s", DEFAULT_CLOCK_TOLERANCE_S))))
        elif kind == "to_webm":
            # One handler for every camera: one table, and the registry maps a table to one.
            videos.append((cfg.get("topic", "/camera/image_raw/compressed"),
                           float(cfg.get("fps", Videos.DEFAULT_FPS))))
        else:
            raise ValueError(f"unknown decoder handler type {kind!r}")
    if videos:
        out.append(Videos(videos))
    return out


def _stem(filename: Optional[str], default: str) -> str:
    if not filename:
        return default
    return filename[:-4] if filename.endswith(".csv") else filename


def plan_for(bag_dir_name: str, recorded: Dict[str, str],
             plugins: Optional[Iterable[dict]] = None,
             taken: Optional[Dict[str, str]] = None) -> Plan:
    """The handlers for a recording named *bag_dir_name* that carries *recorded* topics.

    *taken* is ``{table: role}`` for the tables another recording of the same run already
    gives: a channel here that would fill one of them is reported as not tabulated, for that
    reason, rather than filling the table twice.
    """
    plan = Plan()
    configured = _configured(plugins or ())
    covered = set()
    for handler in configured:
        covered.update(handler.topics())
        plan.handlers.append(handler)

    bag_name = bag_dir_name.rsplit("/", 1)[-1]
    if bag_dir_name == INFRA_BAG:
        if "/rosout" in recorded and "/rosout" not in covered:
            plan.handlers.append(Rosout())
        if "/clock" in recorded and "/clock" not in covered:
            plan.handlers.append(Clock())
    elif bag_dir_name == ROQSIM_BAG:
        _plan_roqsim(plan, recorded, taken or {})
    else:
        if ({"/tf", "/tf_static"} & set(recorded)) and not ({"/tf", "/tf_static"} & covered):
            plan.handlers.append(TfPoses(frames="all"))
        if "/behavior_tree_log" in recorded and "/behavior_tree_log" not in covered:
            plan.handlers.append(Nav2BtLog())
        grids = [t for t, typ in recorded.items()
                 if typ == "nav_msgs/msg/OccupancyGrid" and t not in covered]
        if grids:
            plan.handlers.append(Costmaps(grids))
        actions = sorted({m.group("name") for m in map(_ACTION_TOPIC.match, recorded)
                          if m and m.group(0) not in covered})
        for name in actions:
            plan.handlers.append(ActionTopics(name))
        covered.update(t for h in plan.handlers for t in h.topics())
        generic = []
        for topic, typename in sorted(recorded.items()):
            if topic in covered:
                continue
            if topic in NOT_TABULATED:
                plan.untabulated[topic] = NOT_TABULATED[topic]
            elif typename in BULK_TYPES:
                plan.untabulated[topic] = (f"{typename} is bulk data: read it from the "
                                           f"recording, not from a table")
            else:
                generic.append(topic)
        if generic:
            plan.handlers.append(TopicTable(generic, bag_name=bag_name))

    for handler in plan.handlers:
        for table in handler.tables():
            plan.tables[table] = handler
        if isinstance(handler, TopicTable):
            for topic in handler.topics():
                plan.topic_table[topic] = handler.table_for(topic)
        elif isinstance(handler, ActionTopics):
            feedback, status = handler.tables()
            for topic, table in zip(handler.topics(), (feedback, status)):
                if topic in recorded:
                    plan.topic_table[topic] = table
        else:
            for topic in handler.topics():
                if topic in recorded:
                    plan.topic_table.setdefault(topic, handler.tables()[0])
    return plan


def _plan_roqsim(plan: Plan, recorded: Dict[str, str], taken: Dict[str, str]) -> None:
    """roqsim's recording: its channels and metadata, less what another recording gives."""
    for handler in (SimPoses(), SimJoints(), SimClock()):
        (table,) = handler.tables()
        if handler.TOPIC not in recorded:
            continue
        if table in taken:
            plan.untabulated[handler.TOPIC] = _TAKEN.format(table=table, role=taken[table])
            continue
        plan.handlers.append(handler)
    plan.handlers += [SimRecording(), SimEntities()]
    handled = {t for h in plan.handlers for t in h.topics()}
    for topic in sorted(recorded):
        if topic in handled or topic in plan.untabulated:
            continue
        plan.untabulated[topic] = ROQSIM_NOT_TABULATED.get(
            topic, "no table is defined for this channel of roqsim's recording")


def narrow(plan: Plan, tables: Optional[Iterable[str]]) -> Tuple[List[Handler], List[str]]:
    """The handlers needed for *tables* (all of them for ``None``), and the tables unknown."""
    if tables is None:
        return list(plan.handlers), []
    wanted = list(tables)
    unknown = [t for t in wanted if t not in plan.tables]
    handlers: List[Handler] = []
    seen: List[Handler] = []
    for table in wanted:
        handler = plan.tables.get(table)
        if handler is None or handler in seen:
            continue
        seen.append(handler)
        if isinstance(handler, TopicTable):
            # Only the topics asked for: a topic's table costs its own deserialisation.
            handler = handler.subset([t for t in handler.topics()
                                      if handler.table_for(t) in wanted])
        handlers.append(handler)
    return handlers, unknown


__all__ = ["BULK_TYPES", "INFRA_BAG", "NOT_TABULATED", "Plan", "ROQSIM_BAG",
           "ROQSIM_NOT_TABULATED", "SCENARIO_BAG", "narrow", "plan_for"]
