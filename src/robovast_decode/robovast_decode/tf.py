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

"""A transform buffer with tf2's semantics, in plain Python.

The pose table is resolved the way a ROS node would resolve it -- through a ``tf2`` buffer --
and a table that differs from what ``tf2`` answers would be a different measurement, not a
faster one. So this reproduces the rules that decide *which* samples exist, not only the
arithmetic:

* each frame keeps the transforms to its parent for :data:`DEFAULT_CACHE_TIME` behind the
  newest one; older data is refused on insert and pruned afterwards;
* a transform whose stamp is already stored for that frame replaces the stored one, as
  Jazzy's ``tf2`` does;
* a lookup at a time outside a dynamic frame's stored interval is an
  :class:`ExtrapolationError` -- the latest ``odom -> base_link`` cannot be placed in ``map``
  until a ``map -> odom`` at or after its stamp has arrived;
* between two samples, translation is interpolated linearly and rotation by ``tf2``'s own
  slerp; at a stored stamp the stored value is returned exactly;
* ``/tf_static`` transforms are valid at every time;
* a lookup at time ``0`` means the latest time every edge of the chain has in common.

Frame ids have a leading ``/`` stripped on insert, as ``tf2`` does.
"""

from __future__ import annotations

import bisect
import math
from typing import Dict, List, Optional, Tuple

#: ``tf2``'s default cache length (``BUFFER_CORE_DEFAULT_CACHE_TIME``), in nanoseconds.
DEFAULT_CACHE_TIME = 10_000_000_000

Vec = Tuple[float, float, float]
Quat = Tuple[float, float, float, float]   # x, y, z, w


class TransformError(Exception):
    """A lookup that has no answer."""


class UnknownFrameError(TransformError):
    """A frame the buffer has never heard of."""


class ConnectivityError(TransformError):
    """The two frames are not in one tree."""


class ExtrapolationError(TransformError):
    """The requested time lies outside the data of an edge on the path."""


# -- tf2's quaternion arithmetic, reproduced exactly ---------------------------------------

def _qmul(a: Quat, b: Quat) -> Quat:
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (aw * bx + ax * bw + ay * bz - az * by,
            aw * by + ay * bw + az * bx - ax * bz,
            aw * bz + az * bw + ax * by - ay * bx,
            aw * bw - ax * bx - ay * by - az * bz)


def _qinv(q: Quat) -> Quat:
    return (-q[0], -q[1], -q[2], q[3])


def _qrotate(q: Quat, v: Vec) -> Vec:
    r = _qmul(_qmul(q, (v[0], v[1], v[2], 0.0)), _qinv(q))
    return (r[0], r[1], r[2])


def _dot(a: Quat, b: Quat) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2] + a[3] * b[3]


def _slerp(q: Quat, r: Quat, t: float) -> Quat:
    """``tf2::Quaternion::slerp``: half the shortest-path angle, sign-flipped when needed."""
    s = math.sqrt(_dot(q, q) * _dot(r, r))
    d = _dot(q, r)
    theta = math.acos(max(-1.0, min(1.0, abs(d) / s))) if s else 0.0
    if theta == 0.0:
        return q
    inv = 1.0 / math.sin(theta)
    s0 = math.sin((1.0 - t) * theta)
    s1 = math.sin(t * theta)
    if d < 0:
        return tuple((q[i] * s0 - r[i] * s1) * inv for i in range(4))
    return tuple((q[i] * s0 + r[i] * s1) * inv for i in range(4))


class Transform:
    """``parent -> child`` at one stamp: a translation and a rotation."""

    __slots__ = ("stamp", "parent", "translation", "rotation")

    def __init__(self, stamp: int, parent: str, translation: Vec, rotation: Quat):
        self.stamp = stamp
        self.parent = parent
        self.translation = translation
        self.rotation = rotation


def _interpolate(one: Transform, two: Transform, time: int) -> Transform:
    if one.parent != two.parent:
        return Transform(time, one.parent, one.translation, one.rotation)
    ratio = (time - one.stamp) / (two.stamp - one.stamp)
    rest = 1.0 - ratio
    translation = tuple(rest * a + ratio * b for a, b in zip(one.translation, two.translation))
    return Transform(time, one.parent, translation, _slerp(one.rotation, two.rotation, ratio))


class _TimeCache:
    """One frame's dynamic transforms to its parent, oldest first."""

    def __init__(self, max_storage: int):
        self.max_storage = max_storage
        self.stamps: List[int] = []
        self.data: List[Transform] = []

    def insert(self, tf: Transform) -> bool:
        if self.stamps and self.stamps[-1] > tf.stamp + self.max_storage:
            return False
        i = bisect.bisect_left(self.stamps, tf.stamp)
        if i < len(self.stamps) and self.stamps[i] == tf.stamp:
            self.data[i] = tf
            return True
        self.stamps.insert(i, tf.stamp)
        self.data.insert(i, tf)
        latest = self.stamps[-1]
        cut = 0
        while cut < len(self.stamps) and self.stamps[cut] + self.max_storage < latest:
            cut += 1
        if cut:
            del self.stamps[:cut]
            del self.data[:cut]
        return True

    def get(self, time: int) -> Transform:
        if not self.data:
            raise UnknownFrameError("no data")
        if time == 0:
            return self.data[-1]
        if len(self.data) == 1:
            if self.stamps[0] == time:
                return self.data[0]
            raise ExtrapolationError("only one sample, at another time")
        if time == self.stamps[-1]:
            return self.data[-1]
        if time == self.stamps[0]:
            return self.data[0]
        if time > self.stamps[-1]:
            raise ExtrapolationError("requested time is after the newest sample")
        if time < self.stamps[0]:
            raise ExtrapolationError("requested time is before the oldest sample")
        i = bisect.bisect_right(self.stamps, time) - 1     # newest sample at or before time
        return _interpolate(self.data[i], self.data[i + 1], time)

    def latest(self) -> Tuple[int, Optional[str]]:
        if not self.data:
            return 0, None
        return self.stamps[-1], self.data[-1].parent


class _StaticCache:
    """A ``/tf_static`` transform: the last one given, valid at every time."""

    def __init__(self):
        self.tf: Optional[Transform] = None

    def insert(self, tf: Transform) -> bool:
        self.tf = tf
        return True

    def get(self, time: int) -> Transform:
        if self.tf is None:
            raise UnknownFrameError("no data")
        return Transform(time, self.tf.parent, self.tf.translation, self.tf.rotation)

    def latest(self) -> Tuple[int, Optional[str]]:
        return (0, self.tf.parent) if self.tf is not None else (0, None)


def _strip(frame: str) -> str:
    return frame[1:] if frame.startswith("/") else frame


class TransformBuffer:
    """``tf2``'s ``BufferCore`` for what a pose table needs: insert, and look up by time.

    :meth:`lookup` follows ``BufferCore::walkToTopParent`` step for step, and the time-zero
    case ``getLatestCommonTime``, because which lookups *fail* decides which rows a pose
    table has, and an equivalent-looking walk can fail differently.
    """

    #: ``tf2``'s bound on a quaternion's squared norm before a transform is refused.
    QUATERNION_TOLERANCE = 10e-6

    def __init__(self, cache_time: int = DEFAULT_CACHE_TIME):
        self.cache_time = cache_time
        self._frames: Dict[str, object] = {}
        self._known: set = set()

    def set_transform(self, child: str, parent: str, stamp: int, translation: Vec,
                      rotation: Quat, static: bool = False) -> bool:
        """Insert ``parent -> child``; ``False`` when ``tf2`` would have ignored it."""
        child, parent = _strip(child), _strip(parent)
        if not child or not parent or child == parent:
            return False
        if any(math.isnan(x) for x in (*translation, *rotation)):
            return False
        if abs(_dot(rotation, rotation) - 1.0) > self.QUATERNION_TOLERANCE:
            return False
        self._known.update((child, parent))
        cache = self._frames.get(child)
        if cache is None:
            # A frame keeps the kind of cache it was first given, as tf2's allocateFrame does.
            cache = _StaticCache() if static else _TimeCache(self.cache_time)
            self._frames[child] = cache
        return cache.insert(Transform(stamp, parent, tuple(translation), tuple(rotation)))

    def _gather(self, frame: str, time: int) -> Optional[Transform]:
        try:
            return self._frames[frame].get(time)
        except (ExtrapolationError, UnknownFrameError):
            return None

    def _latest_common_time(self, target: str, source: str) -> int:
        big = float("inf")
        chain = []                      # [(latest stamp, parent)] walked up from the source
        frame = source
        common_time = big
        while frame is not None:
            cache = self._frames.get(frame)
            if cache is None:
                break
            stamp, parent = cache.latest()
            if parent is None:
                break
            if stamp != 0:
                common_time = min(stamp, common_time)
            chain.append((stamp, parent))
            frame = parent
            if frame == target:
                return 0 if common_time == big else common_time
        frame = target
        common_time = big
        common_parent = None
        while True:
            cache = self._frames.get(frame)
            if cache is None:
                break
            stamp, parent = cache.latest()
            if parent is None:
                break
            if stamp != 0:
                common_time = min(stamp, common_time)
            if any(p == parent for _, p in chain):
                common_parent = parent
                break
            frame = parent
            if frame == source:
                return 0 if common_time == big else common_time
        if common_parent is None:
            raise ConnectivityError(f"{source} and {target} are not in one tree")
        for stamp, parent in chain:
            if stamp != 0:
                common_time = min(common_time, stamp)
            if parent == common_parent:
                break
        return 0 if common_time == big else common_time

    def lookup(self, target: str, source: str, time: int) -> Tuple[Vec, Quat]:
        """``target -> source`` at *time* (ns): the pose of *source* in *target*'s frame."""
        vec, quat = self._lookup(target, source, time)
        return vec, _through_matrix(quat)

    def _lookup(self, target: str, source: str, time: int) -> Tuple[Vec, Quat]:
        for frame in (target, source):
            if frame not in self._known:
                raise UnknownFrameError(f"frame {frame} does not exist")
        if source == target:
            return (0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0)
        if time == 0:
            time = self._latest_common_time(target, source)
        source_chain = []
        frame = source
        top_parent = frame
        extrapolated = False
        while True:
            if frame not in self._frames:
                top_parent = frame
                break
            tf = self._gather(frame, time)
            if tf is None:
                top_parent = frame
                extrapolated = True
                break
            if frame == target:
                return _accumulate(source_chain)          # target is a parent of source
            source_chain.append(tf)
            top_parent = frame
            frame = tf.parent
            if len(source_chain) > MAX_GRAPH_DEPTH:
                raise TransformError("the transform tree has a loop")
        target_chain = []
        frame = target
        while frame != top_parent:
            if frame not in self._frames:
                break
            tf = self._gather(frame, time)
            if tf is None:
                raise ExtrapolationError(f"no {frame} -> parent at {time}")
            if frame == source:                               # source is a parent of target
                vec, quat = _accumulate(target_chain)
                inv = _qinv(quat)
                return _qrotate(inv, (-vec[0], -vec[1], -vec[2])), inv
            target_chain.append(tf)
            frame = tf.parent
            if len(target_chain) > MAX_GRAPH_DEPTH:
                raise TransformError("the transform tree has a loop")
        if frame != top_parent:
            if extrapolated:
                raise ExtrapolationError(f"no {top_parent} -> parent at {time}")
            raise ConnectivityError(f"{source} and {target} are not in one tree")
        return _full_path(source_chain, target_chain)


#: ``tf2``'s ``MAX_GRAPH_DEPTH``.
MAX_GRAPH_DEPTH = 1000


def _through_matrix(q: Quat) -> Quat:
    """``tf2::Transform::setRotation`` then ``getRotation``: a quaternion's round trip
    through the 3x3 matrix a ``tf2::Transform`` stores.

    ``BufferCore::lookupTransform`` hands its result back through exactly this, so the
    quaternion a ROS node receives is the matrix's canonical one -- its sign chosen by the
    matrix's trace, its last digits those of the conversion -- not the product the walk
    computed. A pose table that differs from what ``tf2`` answers in the sign of ``w`` is a
    different table, so this is reproduced rather than normalised away.
    """
    x, y, z, w = q
    d = x * x + y * y + z * z + w * w
    s = 2.0 / d
    xs, ys, zs = x * s, y * s, z * s
    wx, wy, wz = w * xs, w * ys, w * zs
    xx, xy, xz = x * xs, x * ys, x * zs
    yy, yz, zz = y * ys, y * zs, z * zs
    m = ((1.0 - (yy + zz), xy - wz, xz + wy),
         (xy + wz, 1.0 - (xx + zz), yz - wx),
         (xz - wy, yz + wx, 1.0 - (xx + yy)))
    trace = m[0][0] + m[1][1] + m[2][2]
    out = [0.0, 0.0, 0.0, 0.0]
    if trace > 0.0:
        r = math.sqrt(trace + 1.0)
        out[3] = r * 0.5
        r = 0.5 / r
        out[0] = (m[2][1] - m[1][2]) * r
        out[1] = (m[0][2] - m[2][0]) * r
        out[2] = (m[1][0] - m[0][1]) * r
    else:
        if m[0][0] < m[1][1]:
            i = 2 if m[1][1] < m[2][2] else 1
        else:
            i = 2 if m[0][0] < m[2][2] else 0
        j = (i + 1) % 3
        k = (i + 2) % 3
        r = math.sqrt(m[i][i] - m[j][j] - m[k][k] + 1.0)
        out[i] = r * 0.5
        r = 0.5 / r
        out[3] = (m[k][j] - m[j][k]) * r
        out[j] = (m[j][i] + m[i][j]) * r
        out[k] = (m[k][i] + m[i][k]) * r
    return (out[0], out[1], out[2], out[3])


def _accumulate(chain) -> Tuple[Vec, Quat]:
    """``top -> start`` for transforms walked upward from *start*: tf2's ``accum``."""
    vec: Vec = (0.0, 0.0, 0.0)
    quat: Quat = (0.0, 0.0, 0.0, 1.0)
    for tf in chain:
        rotated = _qrotate(tf.rotation, vec)
        vec = (rotated[0] + tf.translation[0], rotated[1] + tf.translation[1],
               rotated[2] + tf.translation[2])
        quat = _qmul(tf.rotation, quat)
    return vec, quat


def _full_path(source_chain, target_chain) -> Tuple[Vec, Quat]:
    """tf2's ``finalize(FullPath)``: both chains accumulated to the same top frame."""
    s_vec, s_quat = _accumulate(source_chain)
    t_vec, t_quat = _accumulate(target_chain)
    inv = _qinv(t_quat)
    inv_vec = _qrotate(inv, (-t_vec[0], -t_vec[1], -t_vec[2]))
    rotated = _qrotate(inv, s_vec)
    return ((rotated[0] + inv_vec[0], rotated[1] + inv_vec[1], rotated[2] + inv_vec[2]),
            _qmul(inv, s_quat))


__all__ = ["ConnectivityError", "DEFAULT_CACHE_TIME", "ExtrapolationError", "MAX_GRAPH_DEPTH",
           "TransformBuffer", "TransformError"]
