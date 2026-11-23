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

"""Encode/decode between a typed ``search_space`` and a **normalized** vector.

The vector space is ``[0, 1]^dim`` regardless of each dimension's units. This is
the bridge any vector-based optimizer (pyribs now; CMA-ES/BO later) needs: with a
unit cube, a single scalar step size (``sigma``) is meaningful across dimensions
of very different scales. ``float`` dims map linearly (log dims in log-space),
``int`` dims round/clip/snap to ``step`` on decode, and ``choice`` dims map to an
index bucket in ``[0, 1)``.

A bucket's position carries no meaning, so a distance between two encoded points must not
read it: :meth:`SearchSpaceCodec.distances` measures a ``choice`` or ``bool`` coordinate by
mismatch, 0 for the same value and 1 for any other, and every other coordinate by its
difference in the unit cube. Whatever asks "which evaluated cells are near this one" asks it.
"""

import math
from typing import Any

import numpy as np

from robovast.common.config import BoolDim, ChoiceDim, IntDim


class SearchSpaceCodec:
    def __init__(self, search_space: dict):
        self.paths = list(search_space.keys())     # fixed order = vector layout
        self.dims = [search_space[p] for p in self.paths]
        self.dim = len(self.paths)

    def distances(self, points: np.ndarray, point: np.ndarray) -> np.ndarray:
        """Euclidean distance from each row of ``points`` to ``point``, in the unit cube.

        A ``choice`` or ``bool`` coordinate contributes 0 when both lie in the same value's
        bucket and 1 otherwise: the order of a choice's values is how they were written, not
        a nearness, so the first value is as far from the second as from the last.
        """
        points = np.atleast_2d(np.asarray(points, dtype=float))
        point = np.asarray(point, dtype=float)
        delta = np.abs(points - point)
        for i, dim in enumerate(self.dims):
            if isinstance(dim, (ChoiceDim, BoolDim)):
                k = len(dim.values) if isinstance(dim, ChoiceDim) else 2
                same = (np.minimum(np.floor(points[:, i] * k), k - 1)
                        == min(math.floor(point[i] * k), k - 1))
                delta[:, i] = np.where(same, 0.0, 1.0)
        return np.sqrt((delta ** 2).sum(axis=1))

    def bounds(self) -> tuple[np.ndarray, np.ndarray]:
        """Normalized solution-space bounds: the unit cube."""
        return np.zeros(self.dim), np.ones(self.dim)

    def encode(self, values: dict[str, Any]) -> np.ndarray:
        vec = np.empty(self.dim, dtype=float)
        for i, (path, dim) in enumerate(zip(self.paths, self.dims)):
            vec[i] = self._encode_dim(dim, values[path])
        return vec

    def decode(self, vec: np.ndarray) -> dict[str, Any]:
        return {path: self._decode_dim(dim, float(vec[i]))
                for i, (path, dim) in enumerate(zip(self.paths, self.dims))}

    @staticmethod
    def _encode_dim(dim, v) -> float:
        if isinstance(dim, BoolDim):
            return (int(bool(v)) + 0.5) / 2
        if isinstance(dim, ChoiceDim):
            return (dim.values.index(v) + 0.5) / len(dim.values)
        lo, hi = SearchSpaceCodec._span(dim)
        x = math.log(v) if dim.log else float(v)
        return 0.0 if hi == lo else min(max((x - lo) / (hi - lo), 0.0), 1.0)

    @staticmethod
    def _decode_dim(dim, u: float):
        u = min(max(u, 0.0), 1.0)
        if isinstance(dim, BoolDim):
            return u >= 0.5
        if isinstance(dim, ChoiceDim):
            idx = min(int(math.floor(u * len(dim.values))), len(dim.values) - 1)
            return dim.values[idx]
        lo, hi = SearchSpaceCodec._span(dim)
        x = lo + u * (hi - lo)
        v = math.exp(x) if dim.log else x
        if isinstance(dim, IntDim):
            v = int(round(v))
            if dim.step:
                v = dim.low + round((v - dim.low) / dim.step) * dim.step
            return int(min(max(v, dim.low), dim.high))
        return float(min(max(v, dim.low), dim.high))

    @staticmethod
    def _span(dim) -> tuple[float, float]:
        """Raw-unit (low, high), in log-space when the dim is log-scaled."""
        if dim.log:
            return math.log(dim.low), math.log(dim.high)
        return float(dim.low), float(dim.high)
