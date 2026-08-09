# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The ``videos`` manifest: how a recording is placed on the run's timeline.

``rosbags_to_webm`` re-times every frame onto a constant rate and drops the bag stamps, so the
``.webm`` alone says nothing about *when* its first frame was — a camera that came up ten
seconds into a trial would otherwise replay as though it had run from the start. This manifest
is what carries that, and both readers (the run view's ``camera`` panel and the
``get_camera_frame`` MCP tool) take it from here, so neither can hold a different opinion.

Tested against ``rosbags_common`` rather than the handler wrapping it, for the reason
``test_clock_to_csv_handler`` gives: the handler's module imports ``rosbag2_py`` at load time
and so only exists inside a ROS image, while what is promised here is a file format.
"""

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]
                       / "src" / "robovast" / "results_processing" / "data"))

from rosbags_common import (VIDEO_FIELDNAMES, VIDEOS_CSV,  # noqa: E402
                            register_video)

ROW = {"topic": "/static_camera/image/compressed",
       "file": "rosbag2_static_camera_image_compressed.webm",
       "t_start": 12.5, "t_end": 42.25, "fps": 1.0, "frames": 30}


def _read(run_dir) -> list[dict]:
    with open(Path(run_dir) / VIDEOS_CSV, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_a_video_is_registered_with_the_columns_its_readers_expect(tmp_path):
    register_video(str(tmp_path), ROW)
    rows = _read(tmp_path)
    assert len(rows) == 1
    assert list(rows[0]) == VIDEO_FIELDNAMES, "the column set is the contract"
    assert rows[0]["t_start"] == "12.5", "seconds, like every other table's timestamps"
    assert rows[0]["file"] == ROW["file"], "run-relative, so runFileUrl can address it"


def test_re_registering_the_same_file_replaces_its_row(tmp_path):
    """Postprocessing may be re-run over a directory that already has results.

    Appending would leave two rows for one file, and the readers take the first — so a
    re-encode at a different rate would be described by the *previous* run's numbers.
    """
    register_video(str(tmp_path), ROW)
    register_video(str(tmp_path), {**ROW, "fps": 25.0, "frames": 750})
    rows = _read(tmp_path)
    assert len(rows) == 1, rows
    assert rows[0]["fps"] == "25.0"


def test_two_cameras_share_one_manifest(tmp_path):
    """One file per run directory, not one per video.

    The database builder maps each CSV to a table by its stem and **raises** on two files that
    would claim the same table name, so a second manifest would fail the whole ingest.
    """
    register_video(str(tmp_path), ROW)
    register_video(str(tmp_path), {**ROW, "topic": "/rgbd_camera/image/compressed",
                                   "file": "rosbag2_rgbd_camera_image_compressed.webm"})
    rows = _read(tmp_path)
    assert [r["topic"] for r in rows] == ["/static_camera/image/compressed",
                                          "/rgbd_camera/image/compressed"]


def test_a_partial_row_still_writes_every_column(tmp_path):
    """A producer that knows less than ``rosbags_to_webm`` does is still a valid producer.

    The manifest is a contract any of them may write — a simulator rendering its own video
    knows its rate but has no topic — so a missing key is an empty cell, not a KeyError.
    """
    register_video(str(tmp_path), {"file": "run.webm", "t_start": 0.0})
    rows = _read(tmp_path)
    assert list(rows[0]) == VIDEO_FIELDNAMES
    assert rows[0]["topic"] == "" and rows[0]["fps"] == ""
