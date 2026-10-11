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

"""How a decoded message becomes the columns of a row.

Two flattenings exist because two table families have always been named two ways, and a
table's column names are what every query, panel and notebook addresses it by:

* :func:`table_columns` + :func:`column_values` -- a topic's own table: nested fields joined
  with ``.``, a field declared as an array as **one** list column holding the whole array, a
  sequence of messages as one list column per leaf field. A scan's ranges, a covariance or a
  path's x coordinates is one column, so a table's width follows the message definition and
  not what a run recorded.
* :func:`message_to_dict` + :func:`flatten` -- an action's feedback and status: every level
  joined with ``_``, arrays element by element, a goal id as its hex string and a time as
  seconds.

The clock map's decimation lives here too: its accuracy promise is shared with every reader
of the map, so it has one definition.
"""

from __future__ import annotations

from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

import numpy as np
import pyarrow as pa
from rosbags.typesys.msg import Nodetype

# -- a topic's own table, from the message definition -----------------------------------------

#: A ROS 2 base type -> the Arrow type its column has. ``char``, ``byte`` and ``octet`` are
#: the byte types; a scalar of one is a small integer, a sequence of one is a byte string.
_BASE_TYPES = {
    "bool": pa.bool_(),
    "int8": pa.int8(), "uint8": pa.uint8(), "byte": pa.uint8(), "octet": pa.uint8(),
    "char": pa.uint8(),
    "int16": pa.int16(), "uint16": pa.uint16(),
    "int32": pa.int32(), "uint32": pa.uint32(),
    "int64": pa.int64(), "uint64": pa.uint64(),
    "float32": pa.float32(), "float64": pa.float64(),
    "string": pa.string(), "wstring": pa.string(),
}
_BYTE_TYPES = frozenset({"uint8", "byte", "octet", "char"})


def table_columns(fields_of, typename: str, prefix: str = "") -> List[Tuple[str, pa.DataType]]:
    """``[(column, type)]`` a message of *typename* gives, from its definition alone.

    The rule, applied to each field and recursively:

    * a scalar is one column, named ``a.b.c`` through the nested messages it sits in;
    * a field declared as an array or sequence of numbers, booleans or strings is **one**
      ``list`` column holding the whole array;
    * an array or sequence of ``uint8``/``byte``/``char`` is one ``binary`` column;
    * an array or sequence of messages is one ``list`` column **per leaf field** of the
      element type, the lists of one row aligned by index (``poses.pose.position.x``);
    * a sequence inside a sequence nests the lists.

    So a table's width follows the message definition and never what a run recorded: every
    run of a campaign shares one schema, and a column exists whether or not any message had
    an element for it. *fields_of* maps a type name to its ``rosbags`` field definitions
    (:meth:`~robovast_decode.definitions.TypeCatalog.fields`).
    """
    out: List[Tuple[str, pa.DataType]] = []
    for name, node in fields_of(typename):
        column = f"{prefix}.{name}" if prefix else name
        out.extend(_columns_of(fields_of, column, node))
    return out


def _columns_of(fields_of, column: str, node) -> List[Tuple[str, pa.DataType]]:
    kind, info = node
    if kind == Nodetype.BASE:
        base = info[0]
        if base not in _BASE_TYPES:
            raise ValueError(f"{column}: no column type for the base type {base!r}")
        return [(column, _BASE_TYPES[base])]
    if kind == Nodetype.NAME:
        return table_columns(fields_of, info, column)
    sub = info[0]
    sub_kind, sub_info = sub
    if sub_kind == Nodetype.BASE and sub_info[0] in _BYTE_TYPES:
        return [(column, pa.binary())]
    return [(c, pa.list_(t)) for c, t in _columns_of(fields_of, column, sub)]


def _scalar(value):
    """A decoded scalar as a plain Python value (numpy scalars unwrapped)."""
    if isinstance(value, np.generic):
        return value.item()
    return value


def column_values(fields_of, msg, typename: str, prefix: str = "") -> Iterator[Tuple[str, Any]]:
    """``(column, value)`` per column of :func:`table_columns`, for one message *msg*.

    A list column's value is the whole array (a numpy array or a list); a binary column's is
    ``bytes``; a sequence of messages gives one list per leaf column, in element order.
    """
    for name, node in fields_of(typename):
        column = f"{prefix}.{name}" if prefix else name
        yield from _values_of(fields_of, column, node, getattr(msg, name))


def _values_of(fields_of, column: str, node, value) -> Iterator[Tuple[str, Any]]:
    kind, info = node
    if kind == Nodetype.BASE:
        yield column, _scalar(value)
        return
    if kind == Nodetype.NAME:
        yield from column_values(fields_of, value, info, column)
        return
    sub = info[0]
    sub_kind, sub_info = sub
    if sub_kind == Nodetype.BASE:
        if sub_info[0] in _BYTE_TYPES:
            yield column, (bytes(value) if isinstance(value, (bytes, bytearray))
                           else np.asarray(value, dtype=np.uint8).tobytes())
        elif isinstance(value, np.ndarray):
            yield column, value
        else:
            yield column, [_scalar(v) for v in value]
        return
    # A sequence of messages, or of arrays: the leaf columns of one element, each a list over
    # the elements. Empty when the sequence is, so the columns are there with no elements.
    leaves: Dict[str, list] = {c: [] for c, _ in _columns_of(fields_of, column, sub)}
    for item in value:
        for leaf, leaf_value in _values_of(fields_of, column, sub, item):
            leaves[leaf].append(leaf_value)
    yield from leaves.items()


def message_to_dict(fields_of, msg, typename: str) -> Any:
    """*msg* as nested dicts and lists, for an action's feedback and status.

    A ``unique_identifier_msgs/UUID`` is its hex string and a ``builtin_interfaces`` time or
    duration its seconds, so a goal id reads as one value and a stamp is on the run's clock.
    """
    field_list = fields_of(typename)
    names = [name for name, _ in field_list]
    if names == ["uuid"]:
        return bytearray(np.asarray(msg.uuid, dtype=np.uint8)).hex()
    if names == ["sec", "nanosec"]:
        return msg.sec + msg.nanosec / 1_000_000_000.0
    out = {}
    for name, node in field_list:
        out[name] = _value_to_python(fields_of, getattr(msg, name), node)
    return out


def _value_to_python(fields_of, value, node):
    kind, info = node
    if kind == Nodetype.NAME:
        return message_to_dict(fields_of, value, info)
    if kind in (Nodetype.ARRAY, Nodetype.SEQUENCE):
        sub = info[0]
        if isinstance(value, np.ndarray):
            return value.tolist()
        if isinstance(value, (bytes, bytearray)):
            return list(value)
        return [_value_to_python(fields_of, item, sub) for item in value]
    return _scalar(value)


def flatten(obj: Any, prefix: str = "", sep: str = "_") -> Dict[str, Any]:
    """Nested dicts and lists as one flat ``{column: value}``, keys joined with *sep*."""
    if isinstance(obj, dict):
        result: Dict[str, Any] = {}
        for key, val in obj.items():
            result.update(flatten(val, f"{prefix}{sep}{key}" if prefix else key, sep))
        return result
    if isinstance(obj, list):
        result = {}
        for i, item in enumerate(obj):
            result.update(flatten(item, f"{prefix}{sep}{i}", sep))
        return result
    return {prefix: obj}


# -- the wall<->sim clock map ---------------------------------------------------------------

#: How far the decimated map may mispredict sim time, in seconds. Every kept sample is
#: exact; this bounds the error of what was *dropped*. 5 ms is two orders of magnitude
#: below anything a log line is read at, and it turns a constant-rate run's tens of
#: thousands of ``/clock`` messages into a handful of rows.
DEFAULT_CLOCK_TOLERANCE_S = 0.005

#: How many consecutive samples may be dropped before one is kept regardless: it bounds the
#: per-sample re-check (without it a straight run re-scans an ever-growing buffer) and how
#: much of a long segment one corrupt sample could misrepresent.
_MAX_DROPPED_RUN = 512


class ClockDecimator:
    """Streaming line simplification for ``(wall, sim)`` clock samples.

    ``/clock`` arrives at 100-1000 Hz, and most of what it says is "still the same rate". A
    sample is kept only when dropping it would mispredict sim time by more than *tolerance*
    under the linear interpolation the reader performs -- a promise about accuracy, where a
    fixed-Hz thinning would only be a promise about size. Every dropped sample is re-checked
    against the candidate chord, not just the newest, so a smoothly changing real-time factor
    cannot drift past the tolerance one step at a time.

    :meth:`offer` each sample in wall order, then :meth:`close`, which emits the final sample:
    the map's right edge is where the run stopped, and the reader refuses to extrapolate.
    """

    def __init__(self, tolerance_s: float = DEFAULT_CLOCK_TOLERANCE_S,
                 max_dropped_run: int = _MAX_DROPPED_RUN) -> None:
        self._tolerance = tolerance_s
        self._max_dropped_run = max(1, max_dropped_run)
        self._last_kept: Optional[Tuple[float, float]] = None
        self._buffer: List[Tuple[float, float]] = []
        self.seen: int = 0

    def _chord_fits(self, target: Tuple[float, float]) -> bool:
        w0, s0 = self._last_kept
        w1, s1 = target
        span = w1 - w0
        if span <= 0:
            return False
        rate = (s1 - s0) / span
        return all(abs(sp - (s0 + rate * (wp - w0))) <= self._tolerance
                   for wp, sp in self._buffer[:-1])

    def offer(self, wall: float, sim: float) -> Optional[Tuple[float, float]]:
        """Take one sample; return a sample to write, if this one decided its fate."""
        self.seen += 1
        sample = (wall, sim)
        if self._last_kept is None:
            self._last_kept = sample
            return sample
        self._buffer.append(sample)
        if len(self._buffer) < 2:
            return None
        if self._chord_fits(sample) and len(self._buffer) <= self._max_dropped_run:
            return None
        keep = self._buffer[-2]
        self._last_kept = keep
        self._buffer = [sample]
        return keep

    def close(self) -> Optional[Tuple[float, float]]:
        """The final sample, or ``None`` when it was already written."""
        if not self._buffer:
            return None
        final = self._buffer[-1]
        self._buffer = []
        if final == self._last_kept:
            return None
        self._last_kept = final
        return final

    def run(self, samples: Sequence[Tuple[float, float]]) -> List[Tuple[float, float]]:
        """Decimate a whole sequence -- the non-streaming form."""
        kept = [s for s in (self.offer(w, t) for w, t in samples) if s is not None]
        final = self.close()
        if final is not None:
            kept.append(final)
        return kept


__all__ = ["ClockDecimator", "DEFAULT_CLOCK_TOLERANCE_S", "column_values", "flatten",
           "message_to_dict", "table_columns"]
