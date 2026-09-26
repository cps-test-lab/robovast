# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Bulk data is read from the recording as arrays: images as pixels, clouds as fields, a
topic's messages one at a time in order."""

import numpy as np
import pytest

from robovast_decode import images, points
from robovast_decode.bulk import iter_messages

from .test_frames import (COMPRESSED, RAW, RAW_TOPIC, STORE, TOPIC, closed_bag, compressed,
                          fixture_span, image_bag, jpeg, png, raw)

CLOUD = "sensor_msgs/msg/PointCloud2"
CLOUD_TOPIC = "/points"


def _cloud(t_ns: int, xyz: np.ndarray, intensity: np.ndarray, big_endian: bool = False) -> bytes:
    """CDR bytes of a ``PointCloud2`` with ``x y z`` float32 at 0/4/8 and ``intensity``
    uint16 at 12 in a 16-byte point."""
    order = ">" if big_endian else "<"
    dtype = np.dtype({"names": ["x", "y", "z", "intensity"],
                      "formats": [order + "f4", order + "f4", order + "f4", order + "u2"],
                      "offsets": [0, 4, 8, 12], "itemsize": 16})
    buf = np.zeros(len(xyz), dtype=dtype)
    buf["x"], buf["y"], buf["z"], buf["intensity"] = xyz[:, 0], xyz[:, 1], xyz[:, 2], intensity
    field = STORE.types["sensor_msgs/msg/PointField"]
    header = STORE.types["std_msgs/msg/Header"](
        stamp=STORE.types["builtin_interfaces/msg/Time"](sec=t_ns // 10**9, nanosec=t_ns % 10**9),
        frame_id="lidar")
    msg = STORE.types[CLOUD](
        header=header, height=1, width=len(xyz),
        fields=[field(name="x", offset=0, datatype=7, count=1),
                field(name="y", offset=4, datatype=7, count=1),
                field(name="z", offset=8, datatype=7, count=1),
                field(name="intensity", offset=12, datatype=4, count=1)],
        is_bigendian=big_endian, point_step=16, row_step=16 * len(xyz),
        data=np.frombuffer(buf.tobytes(), dtype=np.uint8), is_dense=True)
    return STORE.serialize_cdr(msg, CLOUD)


# -- images -------------------------------------------------------------------------------

def test_a_raw_image_decodes_to_its_pixels_in_the_encodings_type():
    pixels = np.arange(24 * 32 * 3, dtype=np.uint8).reshape(24, 32, 3)
    got, encoding = images.decode(STORE.deserialize_cdr(raw(0, pixels, "bgr8"), RAW), RAW)
    assert encoding == "bgr8" and got.dtype == np.uint8 and got.shape == (24, 32, 3)
    assert np.array_equal(got, pixels)

    depth = np.linspace(0, 4000, 32 * 24, dtype="<u2").reshape(24, 32)
    got, encoding = images.decode(STORE.deserialize_cdr(raw(0, depth, "16UC1"), RAW), RAW)
    assert encoding == "16UC1" and got.dtype == np.uint16 and np.array_equal(got, depth)

    big = depth.astype(">u2")
    got, _ = images.decode(STORE.deserialize_cdr(raw(0, big, "16UC1", is_bigendian=True), RAW),
                           RAW)
    assert np.array_equal(got, depth), "a big-endian buffer reads as the same numbers"

    ranges = np.linspace(0.0, 5.0, 32 * 24, dtype="<f4").reshape(24, 32)
    got, _ = images.decode(STORE.deserialize_cdr(raw(0, ranges, "32FC1"), RAW), RAW)
    assert got.dtype == np.float32 and np.allclose(got, ranges)

    with pytest.raises(ValueError, match="yuv422"):
        images.decode(STORE.deserialize_cdr(raw(0, depth, "yuv422"), RAW), RAW)


def test_a_compressed_image_decodes_through_its_format():
    got, encoding = images.decode(
        STORE.deserialize_cdr(compressed(0, jpeg((200, 30, 30))), COMPRESSED), COMPRESSED)
    assert encoding == "rgb8" and got.shape == (24, 32, 3) and got[0, 0, 0] > 180
    got, encoding = images.decode(
        STORE.deserialize_cdr(compressed(0, png((30, 200, 30)), "png"), COMPRESSED), COMPRESSED)
    assert encoding == "rgb8" and tuple(got[0, 0]) == (30, 200, 30)


def test_pixels_render_as_a_picture_with_the_channels_in_their_named_order():
    pixels = np.zeros((4, 6, 3), dtype=np.uint8)
    pixels[..., 0] = 250
    assert images.to_pil(pixels, "bgr8").getpixel((0, 0)) == (0, 0, 250)
    assert images.to_pil(pixels, "rgb8").getpixel((0, 0)) == (250, 0, 0)
    depth = np.array([[0, 1000], [2000, 4000]], dtype=np.uint16)
    shown = images.to_pil(depth, "16UC1")
    assert shown.mode == "L" and shown.getpixel((1, 1)) == 255 and shown.getpixel((0, 0)) == 0


# -- point clouds -------------------------------------------------------------------------

@pytest.mark.parametrize("big_endian", [False, True], ids=["little", "big"])
def test_a_point_cloud_decodes_to_one_array_per_field(big_endian):
    xyz = np.array([[1.0, 2.0, 3.0], [4.0, 5.0, 6.0], [np.nan, 0.0, 0.0]], dtype=np.float32)
    intensity = np.array([10, 20, 30], dtype=np.uint16)
    msg = STORE.deserialize_cdr(_cloud(0, xyz, intensity, big_endian), CLOUD)
    fields = points.decode(msg, CLOUD)
    assert set(fields) == {"x", "y", "z", "intensity"}
    assert np.array_equal(fields["intensity"], intensity)
    assert fields["x"].dtype == np.float32 and np.allclose(fields["y"], [2.0, 5.0, 0.0])
    assert points.xyz(fields).shape == (2, 3), "the NaN point is dropped"
    assert points.xyz(fields, keep_nan=True).shape == (3, 3)


def test_a_cloud_without_coordinates_says_what_it_has():
    with pytest.raises(KeyError, match="intensity"):
        points.xyz({"intensity": np.zeros(3)})


# -- the messages of a topic, in order -------------------------------------------------------

def test_a_topics_messages_are_read_in_order_within_the_span_and_thinned(tmp_path):
    t0, _ = fixture_span()
    stamps = [t0 + 10**9 * k for k in range(1, 7)]                # six frames a second apart
    bag = closed_bag(tmp_path / "rosbag2", image_bag(
        {(TOPIC, COMPRESSED): [(t, compressed(t, jpeg((k * 40, 0, 0)))) for k, t in enumerate(stamps)],
         (CLOUD_TOPIC, CLOUD): [(stamps[2], _cloud(stamps[2], np.ones((5, 3), dtype=np.float32),
                                                   np.zeros(5, dtype=np.uint16)))]},
        chunked=True))
    seen = list(iter_messages(str(bag), TOPIC))
    assert [s.t_ns for s in seen] == stamps
    assert seen[0].typename == COMPRESSED and seen[0].t == pytest.approx(stamps[0] / 1e9)
    assert images.decode(seen[3].msg, COMPRESSED)[0][0, 0, 0] > 100

    start, end = stamps[1] / 1e9, stamps[4] / 1e9
    assert [s.t_ns for s in iter_messages(str(bag), TOPIC, start=start, end=end)] == stamps[1:5]
    assert [s.t_ns for s in iter_messages(str(bag), TOPIC, every=2.0)] == stamps[0::2]
    assert [s.t_ns for s in iter_messages(str(bag), TOPIC, start=start, every=2.0)] == stamps[1::2]

    cloud = next(iter_messages(str(bag), CLOUD_TOPIC))
    assert points.xyz(points.decode(cloud.msg, CLOUD)).shape == (5, 3)
    assert not list(iter_messages(str(bag), "/nowhere"))
    assert not list(iter_messages(str(bag), RAW_TOPIC))
