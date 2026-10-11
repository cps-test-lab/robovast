# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A camera topic's frames are found by stamp and read one at a time, never tabulated.

The fixture recording carries no image topic, so the bags here are the fixture's records
with image messages merged in by stamp: ``sensor_msgs/msg/CompressedImage`` and
``sensor_msgs/msg/Image`` under their Jazzy definitions, written chunked or record by
record as a test needs.
"""

import io
import shutil

import numpy as np
import pytest
from mcap.writer import Writer
from PIL import Image as PILImage
from rosbags.typesys import Stores, get_typestore

from robovast_decode.build import find_runs
from robovast_decode.frames import MAX_WIDTH, FrameIndex, FrameTap, to_jpeg
from robovast_decode.framing import Channel, McapTail, Message, Schema
from robovast_decode.live import Session, Watcher

from .conftest import FIXTURE, NAV_CONFIG, make_campaign
from .test_live import GrowingBag, cuts

SEGMENT = FIXTURE / "0" / "rosbag2" / "rosbag2_0.mcap"
COMPRESSED = "sensor_msgs/msg/CompressedImage"
RAW = "sensor_msgs/msg/Image"
TOPIC = "/camera/image_raw/compressed"
RAW_TOPIC = "/camera/image_raw"

STORE = get_typestore(Stores.ROS2_JAZZY)


def _header(t_ns: int):
    return STORE.types["std_msgs/msg/Header"](
        stamp=STORE.types["builtin_interfaces/msg/Time"](sec=t_ns // 10**9,
                                                          nanosec=t_ns % 10**9),
        frame_id="camera")


def jpeg(color=(200, 30, 30), size=(32, 24)) -> bytes:
    out = io.BytesIO()
    PILImage.new("RGB", size, color).save(out, format="JPEG")
    return out.getvalue()


def png(color=(30, 200, 30), size=(32, 24)) -> bytes:
    out = io.BytesIO()
    PILImage.new("RGB", size, color).save(out, format="PNG")
    return out.getvalue()


def compressed(t_ns: int, payload: bytes, fmt: str = "jpeg") -> bytes:
    """CDR bytes of a ``CompressedImage`` stamped *t_ns* holding *payload*."""
    msg = STORE.types[COMPRESSED](header=_header(t_ns), format=fmt,
                                  data=np.frombuffer(payload, dtype=np.uint8))
    return STORE.serialize_cdr(msg, COMPRESSED)


def raw(t_ns: int, pixels: np.ndarray, encoding: str, is_bigendian: bool = False) -> bytes:
    """CDR bytes of an ``Image`` stamped *t_ns*: *pixels* is ``(h, w[, c])`` already laid
    out in *encoding*'s channel order and dtype."""
    height, width = pixels.shape[:2]
    data = np.ascontiguousarray(pixels).tobytes()
    msg = STORE.types[RAW](header=_header(t_ns), height=height, width=width,
                           encoding=encoding, is_bigendian=int(is_bigendian),
                           step=len(data) // height, data=np.frombuffer(data, dtype=np.uint8))
    return STORE.serialize_cdr(msg, RAW)


def fixture_span():
    """``(first, last)`` log time in ns of the fixture's messages."""
    times = [r.log_time for r in McapTail(str(SEGMENT)).read() if isinstance(r, Message)]
    return min(times), max(times)


def image_bag(images: dict, chunked: bool = False, with_fixture: bool = True) -> bytes:
    """An mcap of the fixture's records with *images* merged in by stamp.

    *images* is ``{(topic, typename): [(log_time_ns, cdr bytes), ...]}``. A channel is
    registered with its first message, as rosbag2 writes it.
    """
    messages = []
    schemas, channels = {}, {}
    if with_fixture:
        for record in McapTail(str(SEGMENT)).read():
            if isinstance(record, Schema):
                schemas[record.id] = record
            elif isinstance(record, Channel):
                channels[record.id] = record
            elif isinstance(record, Message):
                channel = channels[record.channel_id]
                schema = schemas[channel.schema_id]
                messages.append((record.log_time, channel.topic, schema.name, schema.data,
                                 channel.message_encoding, record.data))
    for (topic, typename), frames in images.items():
        definition = STORE.generate_msgdef(typename, ros_version=2)[0].encode()
        for t_ns, data in frames:
            messages.append((t_ns, topic, typename, definition, "cdr", data))
    messages.sort(key=lambda m: m[0])
    out = io.BytesIO()
    writer = Writer(out, use_chunking=chunked, chunk_size=1 << 14)
    writer.start(profile="ros2", library="test")
    schema_ids, channel_ids = {}, {}
    for t_ns, topic, typename, definition, encoding, data in messages:
        if topic not in channel_ids:
            if typename not in schema_ids:
                schema_ids[typename] = writer.register_schema(typename, "ros2msg", definition)
            channel_ids[topic] = writer.register_channel(topic, encoding, schema_ids[typename])
        writer.add_message(channel_ids[topic], t_ns, data, t_ns)
    writer.finish()
    return out.getvalue()


def closed_bag(bag_dir, data: bytes):
    """*data* as a closed recording at *bag_dir*, with the fixture's sidecar and metadata."""
    bag_dir.mkdir(parents=True, exist_ok=True)
    (bag_dir / "rosbag2_0.mcap").write_bytes(data)
    shutil.copy(FIXTURE / "0" / "rosbag2" / "message_definitions.json", bag_dir)
    shutil.copy(FIXTURE / "0" / "rosbag2" / "metadata.yaml", bag_dir)
    return bag_dir


def _decoded(data: bytes) -> PILImage.Image:
    image = PILImage.open(io.BytesIO(data))
    image.load()
    return image


# -- the index ----------------------------------------------------------------------------

@pytest.mark.parametrize("chunked", [False, True], ids=["records", "chunks"])
def test_the_index_lists_every_frame_in_order_and_finds_the_nearest(tmp_path, chunked):
    t0, _ = fixture_span()
    stamps = [t0 + 10**9 * k for k in range(1, 6)]
    shades = [40 * k for k in range(1, 6)]
    bag = closed_bag(tmp_path / "rosbag2", image_bag(
        {(TOPIC, COMPRESSED): [(t, compressed(t, jpeg((s, s, s)))) for t, s in zip(stamps, shades)]},
        chunked=chunked))
    index = FrameIndex(str(bag), TOPIC)
    assert index.typename == COMPRESSED and index.encoding == "cdr"
    assert index.times == [t / 1e9 for t in stamps]
    assert index.times == sorted(index.times)
    assert len({(e.segment, e.offset, e.index) for e in index.entries}) == 5, "distinct places"
    if chunked:
        assert any(e.index > 0 for e in index.entries), "messages of one chunk are told apart"

    assert index.nearest(stamps[2] / 1e9 + 0.5) is index.entries[2], "last at or before t"
    assert index.nearest(stamps[2] / 1e9) is index.entries[2], "at t counts"
    assert index.nearest(stamps[0] / 1e9 - 1) is index.entries[0], "before the first: the first"
    assert index.nearest(stamps[-1] / 1e9 + 100) is index.entries[-1]
    assert index.nearest() is index.entries[-1], "no t: the newest"
    for entry, shade in zip(index.entries, shades):
        pixel = _decoded(index.read_frame(entry)).getpixel((5, 5))
        assert abs(pixel[0] - shade) < 4, "each place reads its own frame"
    assert index.extend() == 0, "a closed recording gains nothing"


def test_a_topic_the_recording_does_not_carry_has_no_frames(tmp_path):
    bag = closed_bag(tmp_path / "rosbag2", image_bag({}))
    index = FrameIndex(str(bag), TOPIC)
    assert index.typename is None and index.times == [] and index.nearest(1.0) is None
    assert index.newest_frame() is None


def test_extend_follows_a_recording_as_it_is_written(tmp_path):
    t0, _ = fixture_span()
    stamps = [t0 + 10**9 * k for k in range(1, 9)]
    data = image_bag({(TOPIC, COMPRESSED): [(t, compressed(t, jpeg())) for t in stamps]})
    bag = GrowingBag(tmp_path, data=data)
    first, second, third = cuts(data, 4)
    bag.grow(first)
    index = FrameIndex(str(bag.bag_dir), TOPIC)
    seen = len(index)
    assert seen < len(stamps)
    bag.grow(second)
    added = index.extend()
    assert added >= 0 and len(index) == seen + added
    bag.grow(third)
    bag.close()
    index.extend()
    assert index.times == [t / 1e9 for t in stamps], "the index adds up to the recording"
    assert index.extend() == 0
    stamp, frame = index.newest_frame()
    assert stamp == stamps[-1] / 1e9 and _decoded(frame).size == (32, 24)


# -- one frame ----------------------------------------------------------------------------

def test_a_narrow_jpeg_passes_through_and_a_wide_one_is_downscaled():
    small = jpeg(size=(320, 240))
    msg = STORE.deserialize_cdr(compressed(0, small), COMPRESSED)
    assert to_jpeg(msg, COMPRESSED) == small, "recorded JPEG bytes, untouched"

    wide = jpeg(size=(1280, 400))
    msg = STORE.deserialize_cdr(compressed(0, wide, fmt="rgb8; jpeg compressed bgr8"), COMPRESSED)
    out = _decoded(to_jpeg(msg, COMPRESSED))
    assert out.format == "JPEG" and out.size == (MAX_WIDTH, 200)

    msg = STORE.deserialize_cdr(compressed(0, png(size=(800, 200)), fmt="png"), COMPRESSED)
    out = _decoded(to_jpeg(msg, COMPRESSED))
    assert out.format == "JPEG" and out.size == (MAX_WIDTH, 160), "another format is encoded"
    assert abs(out.getpixel((10, 10))[1] - 200) < 6


def test_a_raw_image_is_encoded_from_its_pixels():
    pixels = np.zeros((300, 800, 3), dtype=np.uint8)
    pixels[..., 0] = 220                                      # red in rgb8
    out = _decoded(to_jpeg(STORE.deserialize_cdr(raw(0, pixels, "rgb8"), RAW), RAW))
    assert out.size == (MAX_WIDTH, 240) and out.getpixel((10, 10))[0] > 200

    out = _decoded(to_jpeg(STORE.deserialize_cdr(raw(0, pixels, "bgr8"), RAW), RAW))
    assert out.getpixel((10, 10))[2] > 200, "bgr8's first channel is blue"

    mono = np.full((24, 32), 128, dtype=np.uint8)
    out = _decoded(to_jpeg(STORE.deserialize_cdr(raw(0, mono, "mono8"), RAW), RAW))
    assert out.mode == "RGB" and abs(out.getpixel((3, 3))[0] - 128) < 4

    depth = np.linspace(0, 4000, 32 * 24, dtype="<u2").reshape(24, 32)
    out = _decoded(to_jpeg(STORE.deserialize_cdr(raw(0, depth, "16UC1"), RAW), RAW))
    assert out.getpixel((0, 0))[0] < out.getpixel((31, 23))[0], "16 bits scaled by their range"

    with pytest.raises(ValueError, match="yuv422"):
        to_jpeg(STORE.deserialize_cdr(raw(0, mono, "yuv422"), RAW), RAW)


def test_a_raw_image_topic_is_indexed_and_read_too(tmp_path):
    t0, _ = fixture_span()
    pixels = np.full((24, 32, 3), (10, 20, 250), dtype=np.uint8)
    bag = closed_bag(tmp_path / "rosbag2", image_bag(
        {(RAW_TOPIC, RAW): [(t0 + 10**9, raw(t0 + 10**9, pixels, "rgb8"))]}, chunked=True))
    index = FrameIndex(str(bag), RAW_TOPIC)
    assert index.typename == RAW and len(index) == 1
    assert _decoded(index.read_frame(index.entries[0])).getpixel((1, 1))[2] > 240


# -- the live path --------------------------------------------------------------------------

def test_a_session_tap_records_every_frame_as_it_advances(tmp_path):
    t0, _ = fixture_span()
    stamps = [t0 + 10**9 * k for k in range(1, 7)]
    data = image_bag({(TOPIC, COMPRESSED): [(t, compressed(t, jpeg((k * 30, 0, 0))))
                                            for k, t in enumerate(stamps, 1)]})
    campaign = make_campaign(tmp_path / "c", verdict=False)
    shutil.rmtree(campaign / "cfg" / "0" / "rosbag2")
    bag = GrowingBag(campaign / "cfg" / "0", data=data)
    (run,) = find_runs(str(campaign))
    session = Session(str(campaign), run, str(bag.bag_dir), [], NAV_CONFIG, frames=[TOPIC])
    assert session.tables == [] and set(session.taps) == {TOPIC}
    tap = session.taps[TOPIC]
    assert isinstance(tap, FrameTap) and tap.newest_frame() is None
    counts = []
    for cut in cuts(data, 5):
        bag.grow(cut)
        assert session.advance() == [], "a tap gives no batch"
        counts.append(len(tap))
    bag.close()
    session.advance()
    assert counts == sorted(counts) and len(tap) == len(stamps)
    assert tap.times == [t / 1e9 for t in stamps]
    stamp, frame = tap.newest_frame()
    assert stamp == stamps[-1] / 1e9
    assert tap.newest_frame()[1] is frame, "the newest JPEG is kept until a newer message"
    assert abs(_decoded(frame).getpixel((3, 3))[0] - 180) < 6
    assert abs(_decoded(tap.read_frame(tap.entries[0])).getpixel((3, 3))[0] - 30) < 6, \
        "an earlier frame is read from its place"


def test_the_watcher_taps_a_run_on_demand_and_lets_it_go_when_the_run_is_done(tmp_path):
    t0, _ = fixture_span()
    stamps = [t0 + 10**9 * k for k in range(1, 5)]
    data = image_bag({(TOPIC, COMPRESSED): [(t, compressed(t, jpeg())) for t in stamps]})
    campaign = make_campaign(tmp_path / "c", verdict=False)
    shutil.rmtree(campaign / "cfg" / "0" / "rosbag2")
    watcher = Watcher(str(campaign), NAV_CONFIG)
    assert watcher.frame_index("cfg/0", TOPIC) is None, "no recording yet"
    bag = GrowingBag(campaign / "cfg" / "0", data=data)
    bag.grow(cuts(data, 2)[0])
    tap = watcher.frame_index("cfg/0", TOPIC)
    assert tap is not None and 0 < len(tap) < len(stamps)
    assert watcher.frame_index("cfg/0", TOPIC) is tap, "demanded once, kept"
    bag.close()
    watcher.changed([str(bag.segment)])
    assert tap.times == [t / 1e9 for t in stamps]
    assert watcher.newest_frame("cfg/0", TOPIC)[0] == stamps[-1] / 1e9
    (campaign / "cfg" / "0" / "test.xml").write_text("<testsuite/>")
    watcher.changed([str(campaign / "cfg" / "0" / "test.xml")])
    assert watcher.following("cfg/0") == set(), "the run is done"
    assert not (campaign / ".cache").exists(), "a tap files no table"
