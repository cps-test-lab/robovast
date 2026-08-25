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

"""Parsing a resource quantity into bytes.

A run's recorded ``available_mem`` does not arrive in one unit. Three producers write it:

* the local lane with no configured limit — ``MemTotal * 1024`` from ``/proc/meminfo``,
  a plain integer of bytes;
* the local lane with ``execution.resources.memory`` set — whatever the ``.vast`` author
  wrote, in Kubernetes quantity syntax (``16Gi``, ``2048Mi``, ``512M``);
* the cluster — the downward API's ``limits.memory``, again a plain integer of bytes.

So the recorded value is a number *or* a suffixed string. Storing that as-is gives a column
that is numeric in some runs and text in others, where ``AVG()`` reads the text rows as 0
and returns a plausible wrong answer. Normalizing at ingest is what makes the column
aggregatable.
"""

import re

#: Kubernetes quantity suffixes. Binary (``Ki``) are powers of 1024; decimal (``k``) are
#: powers of 1000 — a distinction worth keeping, since 16Gi and 16G differ by ~7%.
_BINARY = {"Ki": 1024, "Mi": 1024 ** 2, "Gi": 1024 ** 3,
           "Ti": 1024 ** 4, "Pi": 1024 ** 5, "Ei": 1024 ** 6}
_DECIMAL = {"m": 10 ** -3, "": 1, "k": 10 ** 3, "K": 10 ** 3, "M": 10 ** 6,
            "G": 10 ** 9, "T": 10 ** 12, "P": 10 ** 15, "E": 10 ** 18}

_PATTERN = re.compile(r"^\s*([0-9.]+(?:[eE][-+]?[0-9]+)?)\s*([A-Za-z]*)\s*$")


def to_bytes(value) -> int | None:
    """Parse a Kubernetes-style resource quantity into an integer number of bytes.

    Accepts a plain number (already bytes), or a string with a binary (``Ki``/``Mi``/
    ``Gi``/…) or decimal (``k``/``M``/``G``/…) suffix, or scientific notation.

    Returns ``None`` for anything unparseable — including ``None`` and the empty string.
    A caller records that as "not known", which is honest; guessing a unit would put a
    number that is wrong by a factor of 10^9 into a column that reads as authoritative.

    >>> to_bytes(134603354112)
    134603354112
    >>> to_bytes("16Gi")
    17179869184
    >>> to_bytes("512M")
    512000000
    >>> to_bytes("bogus") is None
    True
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value) if value >= 0 else None
    if not isinstance(value, str):
        return None
    match = _PATTERN.match(value)
    if match is None:
        return None
    number, suffix = match.groups()
    try:
        magnitude = float(number)
    except ValueError:
        return None
    if magnitude < 0:
        return None
    multiplier = _BINARY.get(suffix)
    if multiplier is None:
        multiplier = _DECIMAL.get(suffix)
    if multiplier is None:
        return None
    return int(magnitude * multiplier)


def to_cores(value) -> float | None:
    """Parse a Kubernetes-style CPU quantity into a float number of cores.

    Accepts a plain number of cores (``4``, ``0.5``) or the millicore spelling Kubernetes
    itself uses (``"500m"`` → ``0.5``). Unlike :func:`to_bytes` the binary suffixes are not
    accepted: ``Gi`` of CPU is meaningless, and reading it as a decimal multiplier would
    turn a typo into a reservation big enough that the pod never schedules.

    Returns ``None`` for anything unparseable, so a caller reports "not declared" rather
    than substituting a number nothing measured.

    >>> to_cores(4)
    4.0
    >>> to_cores("500m")
    0.5
    >>> to_cores(0.25)
    0.25
    >>> to_cores("4Gi") is None
    True
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value >= 0 else None
    if not isinstance(value, str):
        return None
    match = _PATTERN.match(value)
    if match is None:
        return None
    number, suffix = match.groups()
    if suffix not in ("", "m"):
        return None
    try:
        magnitude = float(number)
    except ValueError:
        return None
    if magnitude < 0:
        return None
    return magnitude / 1000 if suffix == "m" else magnitude
