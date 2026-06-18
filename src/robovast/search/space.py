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
"""

import math
from typing import Any

import numpy as np

from robovast.common.config import BoolDim, ChoiceDim, FloatDim, IntDim


class SearchSpaceCodec:
    def __init__(self, search_space: dict):
        self.paths = list(search_space.keys())     # fixed order = vector layout
        self.dims = [search_space[p] for p in self.paths]
        self.dim = len(self.paths)

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
