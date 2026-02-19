#!/usr/bin/env python3
# Copyright (C) 2025 Frederik Pasch
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

"""Convert a CompressedImage topic from a rosbag to a WebM video via FFmpeg."""
import argparse
import os
import re
import subprocess
import sys
import time
from multiprocessing import Pool, cpu_count

import rosbag2_py
from rclpy.serialization import deserialize_message
from rosbags_common import find_rosbags
from rosidl_runtime_py.utilities import get_message

from rosbags_common import write_provenance_entry


def sanitize_topic(topic: str) -> str:
    """Convert a topic name like /camera/image_raw/compressed to camera_image_raw_compressed."""
    return re.sub(r"[^a-zA-Z0-9]+", "_", topic).strip("_")


def process_rosbag_wrapper(args):
    """Wrapper function for multiprocessing that unpacks arguments."""
    bag_path, topic, default_fps = args
    return process_rosbag(bag_path, topic, default_fps)


def process_rosbag(bag_path: str, topic: str, default_fps: float) -> int:
    """Process a single rosbag: pipe JPEG frames directly to FFmpeg → WebM.

    Returns:
        Number of frames written, or 0 if topic missing/empty.
    Raises:
        Exception: propagated to the pool caller so the real traceback is visible.
    """
    reader = rosbag2_py.SequentialReader()
    reader.open(
        rosbag2_py.StorageOptions(uri=bag_path, storage_id="mcap"),
        rosbag2_py.ConverterOptions(
            input_serialization_format="cdr", output_serialization_format="cdr"
        ),
    )

    topic_types = reader.get_all_topics_and_types()

    def typename(topic_name):
        for t in topic_types:
            if t.name == topic_name:
                return t.type
        raise ValueError(f"topic {topic_name} not in bag")

    # Check that the requested topic exists
    available_topics = [t.name for t in topic_types]
    if topic not in available_topics:
        print(f"✗ {bag_path}: topic '{topic}' not found (available: {available_topics})", flush=True)
        return 0

    msg_type = get_message(typename(topic))

    # --- First pass: collect timestamps to compute FPS ---
    first_ts = None
    last_ts = None
    frame_count = 0

    while reader.has_next():
        t, data, timestamp = reader.read_next()
        if t != topic:
            continue
        if first_ts is None:
            first_ts = timestamp
        last_ts = timestamp
        frame_count += 1

    if frame_count == 0:
        print(f"✗ {bag_path}: no frames on topic '{topic}'", flush=True)
        return 0

    # Compute FPS from timestamps (nanoseconds)
    if frame_count > 1 and last_ts != first_ts:
        duration_s = (last_ts - first_ts) / 1e9
        fps = (frame_count - 1) / duration_s
    else:
        fps = default_fps

    # --- Build output path ---
    parent_folder = os.path.abspath(os.path.dirname(bag_path))
    bag_name = os.path.basename(bag_path)
    topic_suffix = sanitize_topic(topic)
    output_file = os.path.join(parent_folder, f"{bag_name}_{topic_suffix}.webm")

    # --- Launch FFmpeg: read JPEG frames from stdin, encode to VP9 WebM ---
    # -f image2pipe -vcodec mjpeg: tell FFmpeg the input is a stream of JPEG images
    # -r fps: input frame rate
    # -c:v libvpx-vp9: VP9 encoder (best quality/size for WebM)
    # -crf 10 -b:v 0: constant-quality mode (lossless-ish, adjust crf as needed)
    # -deadline realtime -cpu-used 8: fastest encoding preset
    ffmpeg_cmd = [
        "ffmpeg", "-y",
        "-f", "image2pipe",
        "-vcodec", "mjpeg",
        "-r", f"{fps:.6f}",
        "-i", "pipe:0",
        "-c:v", "libvpx-vp9",
        "-crf", "10",
        "-b:v", "0",
        "-deadline", "realtime",
        "-cpu-used", "8",
        output_file,
    ]

    ffmpeg_proc = subprocess.Popen(
        ffmpeg_cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    # --- Second pass: stream JPEG bytes directly into FFmpeg stdin ---
    reader2 = rosbag2_py.SequentialReader()
    reader2.open(
        rosbag2_py.StorageOptions(uri=bag_path, storage_id="mcap"),
        rosbag2_py.ConverterOptions(
            input_serialization_format="cdr", output_serialization_format="cdr"
        ),
    )

    written = 0
    try:
        while reader2.has_next():
            t, data, _ = reader2.read_next()
            if t != topic:
                continue
            # Check if FFmpeg died before writing the next frame
            if ffmpeg_proc.poll() is not None:
                ffmpeg_stderr = ffmpeg_proc.stderr.read()
                raise RuntimeError(
                    f"FFmpeg exited early (rc={ffmpeg_proc.returncode}) after "
                    f"{written} frames:\n{ffmpeg_stderr.decode(errors='replace')}"
                )
            msg = deserialize_message(data, msg_type)
            # msg.data is a bytes-like array of the raw JPEG/PNG payload
            try:
                ffmpeg_proc.stdin.write(bytes(msg.data))
            except BrokenPipeError as exc:
                ffmpeg_stderr = ffmpeg_proc.stderr.read()
                raise RuntimeError(
                    f"FFmpeg stdin broke after {written} frames:\n"
                    f"{ffmpeg_stderr.decode(errors='replace')}"
                ) from exc
            written += 1
    finally:
        stdin = ffmpeg_proc.stdin
        if stdin is not None:
            try:
                stdin.close()
            except OSError:
                pass  # already closed / broken pipe
            # `subprocess.Popen.communicate()` may attempt to flush stdin if the
            # attribute is set, which can raise "flush of closed file" after we
            # manually close it. Clearing the handle avoids that code path.
            ffmpeg_proc.stdin = None

    _, stderr = ffmpeg_proc.communicate()
    if ffmpeg_proc.returncode != 0:
        raise RuntimeError(f"FFmpeg failed:\n{stderr.decode(errors='replace')}")

    print(f"✓ {output_file}: {written} frames @ {fps:.2f} fps", flush=True)
    return written


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--topic",
        default="/camera/image_raw/compressed",
        help="CompressedImage topic to convert (default: /camera/image_raw/compressed)",
    )
    parser.add_argument(
        "--fps",
        type=float,
        default=30.0,
        help="Fallback FPS when timestamps are unavailable (default: 30)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=cpu_count(),
        help=f"Number of parallel workers (default: {cpu_count()})",
    )
    parser.add_argument(
        "input",
        help="Input directory path to search for rosbags",
    )
    parser.add_argument(
        "--provenance-file",
        default=None,
        help="Write provenance JSON to this path (output/source paths relative to input dir)",
    )

    args = parser.parse_args()

    rosbag_paths = find_rosbags(args.input)

    if not rosbag_paths:
        print(f"No rosbags found in {args.input}")
        return 0

    print(
        f"Found {len(rosbag_paths)} rosbags to process. "
        f"Topic: '{args.topic}', using {args.workers} parallel workers..."
    )

    start = time.time()
    total_frames = 0
    processed_bags = 0
    failed_bags = 0
    error_bags = 0

    process_args = [(bag_path, args.topic, args.fps) for bag_path in rosbag_paths]

    try:
        with Pool(processes=args.workers) as pool:
            results = pool.map(process_rosbag_wrapper, process_args)
    except KeyboardInterrupt:
        print("Processing interrupted by user.")
        return 1
    except Exception as e:
        print(f"Error during processing: {e}")
        return 1

    input_root = os.path.abspath(args.input)
    topic_suffix = sanitize_topic(args.topic)
    for i, frame_count in enumerate(results):
        if frame_count > 0:
            total_frames += frame_count
            processed_bags += 1
            if args.provenance_file:
                bag_path = rosbag_paths[i]
                parent_folder = os.path.abspath(os.path.dirname(bag_path))
                bag_name = os.path.basename(bag_path)
                output_file = os.path.join(parent_folder, f"{bag_name}_{topic_suffix}.webm")
                output_rel = os.path.relpath(output_file, input_root)
                source_rel = os.path.relpath(bag_path, input_root)
                write_provenance_entry(
                    args.provenance_file,
                    output_rel,
                    [source_rel],
                    "rosbags_to_webm",
                    params={"topic": args.topic, "fps": args.fps},
                )
        else:
            failed_bags += 1

    elapsed = time.time() - start
    print(
        f"Summary: {len(rosbag_paths)} rosbags ({processed_bags} success, "
        f"{error_bags} errors, {failed_bags} failed), "
        f"{total_frames} total frames, time {elapsed:.2f}s"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
