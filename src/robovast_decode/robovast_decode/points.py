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

"""One point cloud message as arrays.

A point cloud is bulk data (:data:`~robovast_decode.registry.BULK_TYPES`): a
``sensor_msgs/msg/PointCloud2`` is a byte buffer whose layout its own ``fields`` describe,
and what analysis wants is one array per field -- ``x``, ``y``, ``z``, ``intensity``, ``ring``
-- over the cloud's points. :func:`decode` reads the buffer through a numpy structured
dtype built from that layout, so a cloud of a million points is one ``frombuffer``;
:func:`xyz` stacks the three coordinates as the ``(N, 3)`` array most geometry takes.
The older ``sensor_msgs/msg/PointCloud`` (a list of points and named channels) decodes to
the same shape.
"""

from __future__ import annotations

from typing import Dict

import numpy as np

POINT_CLOUD_TYPES = frozenset({"sensor_msgs/msg/PointCloud2", "sensor_msgs/msg/PointCloud"})

#: ``sensor_msgs/msg/PointField`` datatype constants -> numpy dtype codes.
_DATATYPES = {1: "i1", 2: "u1", 3: "i2", 4: "u2", 5: "i4", 6: "u4", 7: "f4", 8: "f8"}


def decode(msg, typename: str) -> Dict[str, np.ndarray]:
    """``{field: values}`` of one point cloud message, one array of the cloud's points each.

    A field with ``count > 1`` is an ``(N, count)`` array. Nothing is copied but what the
    structured view needs; the byte order is the message's. ``ValueError`` for a field
    whose datatype is not a ``PointField`` one, and for a type that is not a point cloud.
    """
    if typename == "sensor_msgs/msg/PointCloud":
        points = np.asarray([(p.x, p.y, p.z) for p in msg.points], dtype=np.float32).reshape(-1, 3)
        out = {"x": points[:, 0], "y": points[:, 1], "z": points[:, 2]}
        for channel in msg.channels:
            out[channel.name] = np.asarray(channel.values, dtype=np.float32)
        return out
    if typename != "sensor_msgs/msg/PointCloud2":
        raise ValueError(f"{typename} is not a point cloud type")
    order = ">" if msg.is_bigendian else "<"
    names, formats, offsets = [], [], []
    for field in msg.fields:
        if field.datatype not in _DATATYPES:
            raise ValueError(f"field {field.name!r} has PointField datatype {field.datatype}, "
                             "which is not one")
        names.append(field.name)
        count = int(field.count) or 1
        code = order + _DATATYPES[field.datatype]
        formats.append((code, (count,)) if count > 1 else code)
        offsets.append(int(field.offset))
    dtype = np.dtype({"names": names, "formats": formats, "offsets": offsets,
                      "itemsize": int(msg.point_step)})
    count = int(msg.width) * int(msg.height)
    data = msg.data if isinstance(msg.data, np.ndarray) else np.frombuffer(bytes(msg.data), np.uint8)
    points = np.frombuffer(data.tobytes() if data.dtype != np.uint8 else data, dtype=dtype,
                           count=count)
    out = {}
    for name in names:
        values = points[name]
        if values.dtype.byteorder == ">":
            values = values.astype(values.dtype.newbyteorder("="))
        out[name] = values
    return out


def xyz(fields: Dict[str, np.ndarray], keep_nan: bool = False) -> np.ndarray:
    """The ``(N, 3)`` float32 coordinates of a decoded cloud; rows with a NaN coordinate
    dropped unless *keep_nan*, since a cloud spells "no return" with one.

    ``KeyError`` for a cloud without ``x``, ``y`` and ``z``.
    """
    try:
        points = np.stack([np.asarray(fields[c], dtype=np.float32).reshape(-1)
                           for c in ("x", "y", "z")], axis=1)
    except KeyError as exc:
        raise KeyError(f"the cloud has no {exc.args[0]!r} field; it has "
                       f"{', '.join(sorted(fields))}") from exc
    if keep_nan:
        return points
    return points[np.isfinite(points).all(axis=1)]


__all__ = ["POINT_CLOUD_TYPES", "decode", "xyz"]
