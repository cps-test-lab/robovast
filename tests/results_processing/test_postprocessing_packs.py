# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Postprocessing packs the runs' closed recordings before anything reads them, and a file it
could not pack fails it, the file as it was."""

import shutil

from robovast.results_processing import postprocessing
from robovast_decode import pack
from robovast_decode.framing import read_summary
from tests.robovast_decode.conftest import make_campaign
from tests.robovast_decode.test_pack import walked, written_through_bag

from .conftest import write_campaign_db


def _campaign(tmp_path):
    root = make_campaign(tmp_path / "camp-2026-01-01-000000")
    write_campaign_db(root, root.name)
    shutil.rmtree(root / "cfg" / "0" / "rosbag2")
    written_through_bag(root / "cfg" / "0" / "rosbag2")
    (root / "_config").mkdir()
    (root / "_config" / "campaign.vast").write_text(
        "version: 7\nexecution:\n  containers: {}\n"
        "results_processing:\n  postprocessing: []\n")
    return root


def test_postprocessing_packs_every_closed_recording(tmp_path):
    root = _campaign(tmp_path)
    bag = root / "cfg" / "0" / "rosbag2"
    files = sorted(bag.glob("*.mcap")) + sorted((root / "_jobs").rglob("*.mcap"))
    messages = {f: walked(f) for f in files}
    lines = []

    ok, message = postprocessing.run_postprocessing(
        str(tmp_path), campaign=root.name, skip_metadata=True, output_callback=lines.append)

    assert ok, message
    assert any("recording file(s) packed" in line for line in lines)
    for f in files:
        assert read_summary(f).packed and read_summary(f).message_indexed, f
        assert walked(f) == messages[f]


def test_a_recording_that_cannot_be_packed_fails_postprocessing(tmp_path, monkeypatch):
    root = _campaign(tmp_path)
    path = root / "cfg" / "0" / "rosbag2" / "rosbag2_0.mcap"
    data = path.read_bytes()

    def broken(src, dst, chunk_size):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(pack, "_rewrite", broken)
    monkeypatch.setattr(postprocessing, "PACK_WORKERS", 1)
    lines = []

    ok, message = postprocessing.run_postprocessing(
        str(tmp_path), campaign=root.name, skip_metadata=True, output_callback=lines.append)

    assert ok is False
    assert "packing the recordings" in message and "No space left" in message
    assert any("could not be packed and is as it was" in line for line in lines)
    assert path.read_bytes() == data
