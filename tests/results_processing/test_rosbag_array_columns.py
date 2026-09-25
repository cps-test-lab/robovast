# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A numeric array field is one column, and a beam that returned nothing is still a number.

A ``sensor_msgs/LaserScan`` is the case that decides both halves: hundreds of ranges, with
"no return" spelled ``inf``. A column per ray makes a table wider than the index will hold,
and a column that holds ray 417 of every message is not a scan anybody can read; a single
``inf`` written out as a word makes its column text, which takes every number already in it
with it. So the array is stored whole, and what a reader gets back out is the array.

Tested against :mod:`rosbags_common`'s encoding rather than the handler wrapping it: that
handler's module imports ``rosbag2_py`` at load time and so only exists inside a ROS image,
while what is checked here is the shape of the row and the round trip. The CSV the handler
writes is a ``DictWriter`` over exactly these rows, and :func:`_write_csv` is that writer.

The messages are stand-ins carrying the two things the flattener reads off a real one: the
declared field types, and the values. A real ``LaserScan``'s declarations are what they are
spelled with here.
"""

import csv
import math

import pytest

from robovast.results_processing.data.rosbags_common import (decode_numeric_array,
                                                             gen_msg_values,
                                                             is_numeric_array_cell,
                                                             numeric_array_dtype)
from robovast_decode.types import REAL, TEXT, infer_column_types


class _Msg:
    """A ROS message as the flattener sees it: declared field types, and values."""

    def __init__(self, fields: dict, **values):
        self._fields = fields
        for name, value in values.items():
            setattr(self, name, value)

    def get_fields_and_field_types(self) -> dict:
        return self._fields


def _header(frame_id="laser_link"):
    stamp = _Msg({"sec": "int32", "nanosec": "uint32"}, sec=12, nanosec=500_000_000)
    return _Msg({"stamp": "builtin_interfaces/Time", "frame_id": "string"},
                stamp=stamp, frame_id=frame_id)


def _scan(ranges, intensities=None):
    """A ``sensor_msgs/LaserScan``, declared the way the message defines it."""
    return _Msg(
        {"header": "std_msgs/Header", "angle_min": "float", "angle_max": "float",
         "angle_increment": "float", "time_increment": "float", "scan_time": "float",
         "range_min": "float", "range_max": "float", "ranges": "sequence<float>",
         "intensities": "sequence<float>"},
        header=_header(), angle_min=-math.pi, angle_max=math.pi,
        angle_increment=2 * math.pi / len(ranges), time_increment=0.0, scan_time=0.1,
        range_min=0.25, range_max=12.0, ranges=list(ranges),
        intensities=list(intensities if intensities is not None else []))


def _row(msg) -> dict:
    """One CSV row for *msg*, as the handler builds it."""
    return {"timestamp": 1_700_000_000_000_000_000, "type": "LaserScan",
            **dict(gen_msg_values(msg))}


def _write_csv(rows, path) -> list:
    """Write *rows* the way the handler does; return them back as the reader gets them."""
    base = ["timestamp", "type"]
    fieldnames = base + sorted({key for row in rows for key in row} - set(base))
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


# -- what the flattener makes of an array ------------------------------------------------


def test_a_scan_is_one_column_per_field_and_not_one_per_ray():
    """720 rays are one ``ranges`` column, so the table is the message, not the run."""
    row = _row(_scan([1.5] * 720, [0.0] * 720))
    assert "ranges" in row and "ranges[0]" not in row
    assert set(row) == {
        "timestamp", "type", "header.stamp.sec", "header.stamp.nanosec",
        "header.frame_id", "angle_min", "angle_max", "angle_increment",
        "time_increment", "scan_time", "range_min", "range_max", "ranges", "intensities"}


def test_two_scans_of_different_lengths_are_one_table():
    """The declared type decides the shape, so a sensor that reports fewer rays after a
    restart still lands in the same columns."""
    assert set(_row(_scan([1.0] * 720))) == set(_row(_scan([1.0] * 271)))


def test_the_ranges_read_back_exactly():
    """Storing it compactly is worth nothing if nothing can read it back."""
    ranges = [0.5, 1.25, 2.75, 12.0]
    stored = _row(_scan(ranges))["ranges"]
    assert is_numeric_array_cell(stored)
    assert decode_numeric_array(stored).tolist() == ranges


def test_a_beam_that_returned_nothing_comes_back_as_infinity():
    """``inf`` is data — the reading that says the beam hit nothing within range."""
    values = decode_numeric_array(_row(_scan([1.5, math.inf, -math.inf, math.nan]))["ranges"])
    assert values[0] == 1.5
    assert math.isinf(values[1]) and values[1] > 0
    assert math.isinf(values[2]) and values[2] < 0
    assert math.isnan(values[3])


def test_an_empty_array_is_an_empty_array():
    """A scan with no intensities still has the column, holding nothing."""
    assert decode_numeric_array(_row(_scan([1.0, 2.0]))["intensities"]).tolist() == []


def test_a_fixed_size_array_is_stored_the_same_way():
    """A covariance is declared ``double[36]``, and is an array like any other."""
    covariance = [float(i) / 8 for i in range(36)]
    msg = _Msg({"covariance": "double[36]"}, covariance=covariance)
    assert decode_numeric_array(dict(gen_msg_values(msg))["covariance"]).tolist() == covariance


def test_a_byte_array_is_stored_the_same_way():
    """An ``octet`` field arrives as bytes rather than as a sequence of numbers."""
    msg = _Msg({"payload": "sequence<octet>"}, payload=b"\x00\x2a\xff")
    stored = dict(gen_msg_values(msg))["payload"]
    assert decode_numeric_array(stored).tolist() == [0, 42, 255]


def test_a_sequence_of_sub_messages_still_gets_a_column_per_element():
    """Nothing packs a pose, and a handful of waypoints is what those columns are for."""
    def point(x):
        return _Msg({"x": "double", "y": "double"}, x=x, y=0.0)

    path = _Msg({"points": "sequence<geometry_msgs/Point>"}, points=[point(1.0), point(2.0)])
    assert dict(gen_msg_values(path)) == {"points[0].x": 1.0, "points[0].y": 0.0,
                                          "points[1].x": 2.0, "points[1].y": 0.0}


def test_only_a_numeric_element_type_makes_an_array_column():
    """The declaration is read, not guessed: every way of declaring numbers, and nothing else."""
    assert numeric_array_dtype("sequence<float>") == "f4"
    assert numeric_array_dtype("sequence<double, 9>") == "f8"
    assert numeric_array_dtype("double[36]") == "f8"
    assert numeric_array_dtype("uint8[16]") == "u1"
    assert numeric_array_dtype("boolean[4]") == "b1"
    assert numeric_array_dtype("sequence<string>") is None
    assert numeric_array_dtype("sequence<geometry_msgs/Point>") is None
    assert numeric_array_dtype("double") is None


def test_a_cell_that_is_not_an_array_refuses_to_be_read_as_one():
    """A wrong expectation says so, rather than answering with a plausible empty array."""
    for cell in ("1.5", "", None, "num1:f4:2"):
        with pytest.raises(ValueError):
            decode_numeric_array(cell)


# -- what the ingest then types ----------------------------------------------------------


def test_one_beam_with_no_return_does_not_turn_the_scan_table_to_text(tmp_path):
    """The whole point: the numbers beside the array stay numbers, whatever the sensor saw."""
    rows = _write_csv([_row(_scan([1.5, math.inf, 3.0])), _row(_scan([math.inf] * 3))],
                      tmp_path / "scan.csv")
    types = infer_column_types(rows, rows[0].keys())
    assert types["range_max"] == REAL
    assert types["angle_min"] == REAL
    assert types["ranges"] == TEXT, "the packed array is one text cell, by construction"
    assert [column for column, type_ in types.items() if type_ == TEXT] == [
        "type", "header.frame_id", "intensities", "ranges"]


def test_a_non_finite_scalar_stays_a_number(tmp_path):
    """A ``sensor_msgs/Range`` reporting no return is written as the infinity it is, and the
    column stays numeric -- distinct from an empty cell, which means nothing was read."""
    def ranged(value):
        return {"timestamp": 1, "type": "Range",
                **dict(gen_msg_values(_Msg({"range": "float"}, range=value)))}

    rows = _write_csv([ranged(0.8), ranged(math.inf), ranged(math.nan)],
                      tmp_path / "range.csv")
    assert [row["range"] for row in rows] == ["0.8", "inf", "nan"]
    assert infer_column_types(rows, rows[0].keys())["range"] == REAL


def test_a_scan_survives_the_round_trip_through_the_file(tmp_path):
    """End to end: the message the bag held, the CSV the step writes, the array a reader gets."""
    ranges = [0.5, math.inf, 2.25] * 240
    rows = _write_csv([_row(_scan(ranges))], tmp_path / "scan.csv")
    read_back = decode_numeric_array(rows[0]["ranges"])
    assert len(read_back) == 720
    assert [v for v in read_back.tolist() if math.isfinite(v)] == [
        v for v in ranges if math.isfinite(v)]
    assert sum(1 for v in read_back.tolist() if math.isinf(v)) == 240
    assert float(rows[0]["range_max"]) == 12.0
