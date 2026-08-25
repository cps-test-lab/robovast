# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Resource quantities normalize to bytes, or to nothing.

A run's ``available_mem`` arrives as a plain byte count from ``/proc/meminfo`` or the
Kubernetes downward API, and as a suffixed string when the ``.vast`` set a limit. Both
must land in one comparable column, and an unparseable value must land as NULL rather than
as a number that is wrong by a factor of 10^9.
"""

import pytest

from robovast.common.quantity import to_bytes, to_cores


@pytest.mark.parametrize("value,expected", [
    # Already bytes — /proc/meminfo * 1024, and the downward API's limits.memory.
    (134603354112, 134603354112),
    ("134603354112", 134603354112),
    (0, 0),
    # Binary suffixes: what a .vast writes (`memory: 16Gi`).
    ("16Gi", 16 * 1024 ** 3),
    ("2048Mi", 2048 * 1024 ** 2),
    ("512Ki", 512 * 1024),
    ("1Ti", 1024 ** 4),
    # Decimal suffixes are a different number, and must not be conflated with binary.
    ("512M", 512 * 10 ** 6),
    ("16G", 16 * 10 ** 9),
    ("1k", 1000),
    # Fractional and exponent forms Kubernetes accepts.
    ("1.5Gi", int(1.5 * 1024 ** 3)),
    ("1e3", 1000),
    # Surrounding whitespace is not a parse failure.
    ("  8Gi  ", 8 * 1024 ** 3),
])
def test_parses_to_bytes(value, expected):
    assert to_bytes(value) == expected


def test_binary_and_decimal_suffixes_are_not_the_same_number():
    """16Gi and 16G differ by ~7%; treating them alike would silently misreport memory."""
    assert to_bytes("16Gi") != to_bytes("16G")
    assert to_bytes("16Gi") > to_bytes("16G")


@pytest.mark.parametrize("value", [
    None, "", "   ", "bogus", "16Xi", "Gi", "-1", -5, True, False, [], {},
])
def test_unparseable_is_none_not_a_guess(value):
    """None means "not recorded". Guessing a unit would put an authoritative-looking
    wrong number into a column an analysis averages."""
    assert to_bytes(value) is None


@pytest.mark.parametrize("value,expected", [
    # Whole cores, the form every existing .vast uses.
    (4, 4.0),
    ("4", 4.0),
    (0, 0.0),
    # Fractional cores: the point of the exercise. On the cluster a campaign's throughput is
    # quota // pod_request, so rounding a 0.3-core sidecar up to a whole core is paid per job.
    (0.5, 0.5),
    (4.75, 4.75),
    ("0.25", 0.25),
    # Millicores: what Kubernetes itself writes, so a .vast author may well write it too.
    ("500m", 0.5),
    ("250m", 0.25),
    ("1000m", 1.0),
])
def test_to_cores_parses_cores_and_millicores(value, expected):
    assert to_cores(value) == pytest.approx(expected)


@pytest.mark.parametrize("value", [
    None, "", "lots", "  ", [], {},
    # Byte suffixes are not CPU quantities. Accepting "4Gi" as 4e9 cores would turn a typo into
    # a reservation no cluster can schedule, and the pod would simply never start.
    "4Gi", "512M", "16G",
    # A negative reservation is not a reservation.
    -1, "-2",
    # bool is an int subclass; True must not read as one core.
    True, False,
])
def test_to_cores_rejects_what_is_not_a_cpu_quantity(value):
    assert to_cores(value) is None
