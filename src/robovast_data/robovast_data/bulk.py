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

"""A run's images and point clouds, as arrays, read from its recording.

Bulk data never becomes rows (:data:`~robovast_decode.registry.BULK_TYPES`); it is addressed
by run, topic and time. A :class:`Frame` is one image with its pixels in the encoding's own
type, a :class:`PointCloud` one cloud with one array per field. Both carry the message's
stamp in the seconds ``poses`` uses (``t``) and in the nanoseconds a topic's own table uses
(``t_ns``), so what a loop over them produces joins the tables on ``timestamp``.

The functions here read one recording; :class:`~robovast_data.Data` names the run and a
:class:`~robovast_data.RemoteCampaign` asks a service for the same things.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, Iterator, Optional

import numpy as np
from robovast_decode import images, points
from robovast_decode.build import find_runs, scenario_recording
from robovast_decode.bulk import Sample, iter_messages, nearest_message


@dataclass(frozen=True)
class Frame:
    """One image of a run: its pixels, and when and where it was taken."""
    #: The stamp in seconds, the clock ``poses`` and the frame route use.
    t: float
    #: The same stamp in nanoseconds, as a topic's own table carries ``timestamp``.
    t_ns: int
    topic: str
    #: ``(height, width)`` or ``(height, width, channels)``, in the encoding's own dtype and
    #: channel order (``bgr8`` is blue first).
    image: np.ndarray = field(repr=False)
    #: The ``sensor_msgs`` encoding name the pixels are in.
    encoding: str
    frame_id: str

    @property
    def shape(self):
        return self.image.shape

    def pil(self):
        """The frame as a Pillow image a notebook shows: channels in their named order, a
        depth image scaled to 8 bits by its range."""
        return images.to_pil(self.image, self.encoding)


@dataclass(frozen=True)
class PointCloud:
    """One point cloud of a run: one array per field, and its coordinates stacked."""
    t: float
    t_ns: int
    topic: str
    #: ``{field: values}`` over the cloud's points, each in the field's own dtype.
    fields: Dict[str, np.ndarray] = field(repr=False)
    frame_id: str
    #: Whether :attr:`xyz` keeps rows with a NaN coordinate, which is how a cloud spells "no
    #: return".
    keep_nan: bool = False

    @property
    def xyz(self) -> np.ndarray:
        """The ``(N, 3)`` float32 coordinates."""
        return points.xyz(self.fields, keep_nan=self.keep_nan)

    def __len__(self) -> int:
        first = next(iter(self.fields.values()), None)
        return 0 if first is None else len(first)


def run_recording(campaign_dir: str, config_name: str, run_id: int) -> str:
    """The scenario recording of run *config_name*/*run_id* of the campaign at *campaign_dir*.

    ``KeyError`` for a run the campaign does not have; ``FileNotFoundError`` for a copy of the
    campaign that has the run and not its recording -- an export without ``--bags``, say --
    which is where a frame would come from and nothing else can stand in for.
    """
    key = f"{config_name}/{run_id}"
    matching = [r for r in find_runs(campaign_dir) if r.key == key]
    if not matching:
        raise KeyError(f"{os.path.basename(campaign_dir)} has no run {key}")
    bag_dir = scenario_recording(matching[0])
    if bag_dir is None:
        raise FileNotFoundError(
            f"run {key} of {os.path.basename(campaign_dir)} has no recording in this copy of "
            "the campaign, and images and point clouds are read from the recording: download "
            "the campaign (`vast campaign download`) or export it with `--bags mcap`")
    return bag_dir


def _frame(topic: str, sample: Sample) -> Frame:
    if sample.typename not in images.IMAGE_TYPES:
        raise ValueError(f"{topic} carries {sample.typename}, not an image")
    pixels, encoding = images.decode(sample.msg, sample.typename)
    return Frame(sample.t, sample.t_ns, topic, pixels, encoding,
                 str(getattr(getattr(sample.msg, "header", None), "frame_id", "")))


def _cloud(topic: str, sample: Sample, keep_nan: bool) -> PointCloud:
    if sample.typename not in points.POINT_CLOUD_TYPES:
        raise ValueError(f"{topic} carries {sample.typename}, not a point cloud")
    return PointCloud(sample.t, sample.t_ns, topic, points.decode(sample.msg, sample.typename),
                      str(getattr(getattr(sample.msg, "header", None), "frame_id", "")), keep_nan)


def frames(bag_dir: str, topic: str, start: Optional[float] = None, end: Optional[float] = None,
           every: Optional[float] = None) -> Iterator[Frame]:
    """Every frame of *topic* in the recording at *bag_dir*, in order, one at a time.

    *start* and *end* bound the stamps in seconds; *every* keeps one frame per that many
    seconds. Nothing for a topic the recording does not carry.
    """
    for sample in iter_messages(bag_dir, topic, start, end, every):
        yield _frame(topic, sample)


def frame(bag_dir: str, topic: str, t: Optional[float] = None) -> Frame:
    """The frame of *topic* at or before *t*, the first when none is, the last for ``None``.

    ``KeyError`` for a recording without the topic.
    """
    sample = nearest_message(bag_dir, topic, t)
    if sample is None:
        raise KeyError(f"the recording at {bag_dir} has no frame of {topic}")
    return _frame(topic, sample)


def pointclouds(bag_dir: str, topic: str, start: Optional[float] = None,
                end: Optional[float] = None, every: Optional[float] = None,
                keep_nan: bool = False) -> Iterator[PointCloud]:
    """Every cloud of *topic* in the recording at *bag_dir*, in order, one at a time."""
    for sample in iter_messages(bag_dir, topic, start, end, every):
        yield _cloud(topic, sample, keep_nan)


def pointcloud(bag_dir: str, topic: str, t: Optional[float] = None,
               keep_nan: bool = False) -> PointCloud:
    """The cloud of *topic* at or before *t*, the first when none is, the last for ``None``."""
    sample = nearest_message(bag_dir, topic, t)
    if sample is None:
        raise KeyError(f"the recording at {bag_dir} has no point cloud of {topic}")
    return _cloud(topic, sample, keep_nan)


__all__ = ["Frame", "PointCloud", "frame", "frames", "pointcloud", "pointclouds", "run_recording"]
