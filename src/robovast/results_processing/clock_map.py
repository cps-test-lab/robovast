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

"""Wall time → sim time, for a single run.

Everything a run *logs* is stamped in wall time: rosout's receive time, and whatever
each container printed. Everything a run is *analysed* on is sim seconds — the rosbag's
receive time under ``use_sim_time``, which is what the run view's playback bar scrubs.
Relating the two is this module's whole job.

**A single offset is wrong.** Sim time does not run at wall rate: measured on one
recorded run, ``dt/dw = 1.369`` — the simulator ran 1.37× faster than the wall clock, so
an offset taken at the start is ~8 s out by the end of a 29 s run. It can also pause. So
the relation is carried as *samples* and interpolated piecewise-linearly between them.

**Outside the sampled range there is no answer, and none is invented.** The samples come
from ``/clock``, so they begin when the simulator started publishing it — typically well
after the container did. A line logged during image boot or stack bring-up has no sim
time because sim time did not exist yet, which is a different statement from "we could
not compute it". :meth:`ClockMap.to_sim` returns ``None`` there; callers keep the wall
stamp and say so, rather than extrapolating backwards into a clock that was not running.
"""

from __future__ import annotations

import bisect
import csv
import os
from typing import List, NamedTuple, Optional, Sequence, Tuple

# The writer's half of this contract lives in ``data/rosbags_common.py``, because
# ``rosbags_process.py`` is copied into the container as a standalone script and can import
# nothing from this package. Re-exported here so host-side callers have one import site and
# there is still only one definition of the tolerance and the column names.
# pylint: disable=unused-import  # the four below are the documented re-export
from .data.rosbags_common import CLOCK_MAP_FIELDNAMES as FIELDNAMES  # noqa: F401
from .data.rosbags_common import CLOCK_MAP_FILENAME as FILENAME  # noqa: F401
from .data.rosbags_common import DEFAULT_CLOCK_TOLERANCE_S as DEFAULT_TOLERANCE_S  # noqa: F401
from .data.rosbags_common import ClockDecimator as Decimator  # noqa: F401

#: ``clock_map_source`` values. ``none`` is a finding, not a default: it says this run's
#: log lines are wall-time only, and every surface that shows them has to say so.
SOURCE_NONE = "none"
SOURCE_ROS_CLOCK_BAG = "ros_clock_bag"
SOURCE_RST_RUN_NPZ = "roqsim_run_npz"


class ClockMapInfo(NamedTuple):
    """What a map is worth, for the ``runs`` table and any reader deciding to trust it.

    ``wall_span_s`` is the wall interval the samples actually cover — the window in which
    a log line can be given a sim time at all.

    Deliberately **not** a max-gap figure. The samples are decimated (a sample is dropped
    when linear interpolation reproduces it within the writer's tolerance), so a long wall
    gap between two kept samples means "the rate was steady here", not "data is missing" —
    a gap metric would report the healthiest stretch of a run as its worst.
    """
    source: str
    samples: int
    wall_span_s: float


class ClockMap:
    """Wall→sim for one run, from ``(wall_ts, sim_ts)`` samples.

    Samples must be sorted by ``wall_ts``; :func:`load_clock_map` sorts them.
    """

    def __init__(self, samples: Sequence[Tuple[float, float]],
                 source: str = SOURCE_ROS_CLOCK_BAG) -> None:
        self._wall: List[float] = [w for w, _ in samples]
        self._sim: List[float] = [s for _, s in samples]
        self._source = source if self._wall else SOURCE_NONE

    def __bool__(self) -> bool:
        """Truthy only when the map can answer anything — two samples define a rate."""
        return len(self._wall) >= 2

    @property
    def info(self) -> ClockMapInfo:
        span = (self._wall[-1] - self._wall[0]) if len(self._wall) >= 2 else 0.0
        return ClockMapInfo(self._source, len(self._wall), span)

    def to_sim(self, wall: Optional[float]) -> Optional[float]:
        """Sim seconds at *wall*, or ``None`` when that is outside the sampled range."""
        if wall is None or len(self._wall) < 2:
            return None
        if wall < self._wall[0] or wall > self._wall[-1]:
            return None
        i = bisect.bisect_left(self._wall, wall)
        if self._wall[i] == wall:
            return self._sim[i]
        w0, w1 = self._wall[i - 1], self._wall[i]
        s0, s1 = self._sim[i - 1], self._sim[i]
        span = w1 - w0
        if span <= 0:
            return s1
        return s0 + (s1 - s0) * ((wall - w0) / span)


#: The empty map, for a run with no ``/clock`` and no roqsim samples. Returned rather than
#: ``None`` so callers do not each re-invent "what if there is no map".
NO_CLOCK_MAP = ClockMap([], SOURCE_NONE)


#: What roqsim names its streamed clock record: ``run.npz`` -> ``run.clock_map.csv``. Same two
#: columns as the ROS one, and epoch on the wall axis for the same reason — a reader outside the
#: simulator's process has calendar stamps, not that process's monotonic origin.
ROQSIM_SUFFIX = ".clock_map.csv"


def find_run_clock_map(run_dir: str) -> ClockMap:
    """The clock map a **non-ROS** run left beside its recording, or :data:`NO_CLOCK_MAP`.

    Written line by line as the run proceeds (``roqsim.capture``), so unlike the ``.npz`` it
    survives a run killed by a timeout — which is the run whose log most needs placing in time.
    """
    try:
        names = sorted(os.listdir(run_dir))
    except OSError:
        return NO_CLOCK_MAP
    for name in names:
        if name.endswith(ROQSIM_SUFFIX):
            found = load_clock_map(os.path.join(run_dir, name), SOURCE_RST_RUN_NPZ)
            if found:
                return found
    return NO_CLOCK_MAP


def load_clock_map(path: str, source: str = SOURCE_ROS_CLOCK_BAG) -> ClockMap:
    """Read a ``clock_map.csv``; :data:`NO_CLOCK_MAP` when it is absent or unusable.

    A missing file is the normal case for a non-ROS run and for a campaign whose
    postprocessing predates the ``clock_to_csv`` handler, so it is not an error here — it
    becomes the ``none`` provenance the reader is told about.
    """
    if not path or not os.path.isfile(path):
        return NO_CLOCK_MAP
    samples: List[Tuple[float, float]] = []
    try:
        with open(path, newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                try:
                    samples.append((float(row["wall_ts"]), float(row["sim_ts"])))
                except (KeyError, TypeError, ValueError):
                    continue
    except OSError:
        return NO_CLOCK_MAP
    if len(samples) < 2:
        return NO_CLOCK_MAP
    samples.sort(key=lambda pair: pair[0])
    return ClockMap(samples, source)
