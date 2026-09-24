# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A campaign directory assembled from the ``nav_run`` fixture recording, and one from a
recording in roqsim's own shape."""

import json
import os
import shutil
from pathlib import Path

import pytest
import yaml

FIXTURE = Path(__file__).parent / "fixtures" / "nav_run"

#: The decoder configuration the fixture's expected tables were produced with.
NAV_CONFIG = {"groups": [
    {"bag_dir": "rosbag2", "plugins": [
        {"type": "tf_to_csv", "frames": "all", "require": ["base_link", "robot_gt"]},
        {"type": "nav2_bt_to_csv"},
        {"type": "costmap_to_csv", "topics": ["/global_costmap/costmap"]},
        {"type": "to_csv", "topics": ["/collision", "/scan"]},
        {"type": "action_to_csv", "action": "navigate_to_pose"}]},
    {"bag_dir": "logs/rosout_bag", "plugins": [
        {"type": "rosout_to_csv"}, {"type": "clock_to_csv"}]}]}


def make_campaign(root: Path, runs=(("cfg", 0),), shared_job=False, verdict=True) -> Path:
    """A campaign whose every run carries the fixture recording and its job's.

    Jobs are linked the way a campaign links them before any job has ended: through
    ``_transient/job_links.yaml`` alone, with no ``job`` symlink beside the run.
    """
    links = {}
    for i, (config, run_id) in enumerate(runs):
        run = root / config / str(run_id)
        run.mkdir(parents=True)
        shutil.copytree(FIXTURE / "0" / "rosbag2", run / "rosbag2")
        job = root / "_jobs" / ("job-0" if shared_job else f"job-{i}")
        if not (job / "logs" / "rosout_bag").exists():
            (job / "logs").mkdir(parents=True, exist_ok=True)
            shutil.copytree(FIXTURE / "0" / "logs" / "rosout_bag", job / "logs" / "rosout_bag")
        links[f"{config}/{run_id}/job"] = os.path.relpath(job, run)
        if verdict:
            (run / "test.xml").write_text("<testsuite/>")
    (root / "_transient").mkdir(parents=True, exist_ok=True)
    (root / "_transient" / "job_links.yaml").write_text(yaml.safe_dump(links))
    return root


@pytest.fixture
def campaign(tmp_path) -> Path:
    return make_campaign(tmp_path / "nav-2026-01-01-00000000")


# -- roqsim's own recording -------------------------------------------------------------------

#: What the fixture recording holds: the bodies and joints, the sample rate as ``[num, den]``,
#: and the wall clock it starts at.
ROQSIM_BODIES = ("robot", "box", "shelf")
ROQSIM_JOINTS = ("wheel_left", "wheel_right")
ROQSIM_FPS = (50, 1)
ROQSIM_SAMPLES = 50
ROQSIM_WALL_START = 1_786_224_804.0
#: The recording's provenance as roqsim writes it at start; the closing record adds to it.
ROQSIM_META = {
    "format_version": 3, "seed": 7, "episode": 0, "world": "worlds/open_space.yaml",
    "overrides": {"robot.speed": 0.5}, "packages": {"roqsim": "0.9.0", "mujoco": "3.2.0"},
    "state_size": 4, "state_fields": ["qpos", "qvel"], "dtype": "float32",
    "capture_fps": list(ROQSIM_FPS), "capture_every_steps": 10, "timestep": 0.002,
    "camera_track": None, "wall_clock_origin": 0.0, "wall_start_epoch": ROQSIM_WALL_START,
    "model": {"nbody": 3, "njnt": 2},
}
ROQSIM_META_CLOSE = {**ROQSIM_META, "endpoint_rates": {"poses": 50.0},
                     "model": {"nbody": 3, "njnt": 2, "nq": 2}}
ROQSIM_ROSTER_START = [{"name": n, "kind": "prop" if n != "robot" else "robot", "body": n,
                        "present": True} for n in ROQSIM_BODIES]
ROQSIM_ROSTER_END = [{**e, "present": e["name"] != "box"} for e in ROQSIM_ROSTER_START]


def roqsim_wall_time(t: float, span: float) -> float:
    """The wall epoch of sim second *t* in a recording spanning *span* sim seconds.

    Sim runs at 1.25x wall time for the first half, then pauses half a wall second and runs
    at wall rate: a rate change and a pause, so the decimated clock map keeps more than its
    two ends and a reader can tell the two stretches apart.
    """
    half = span / 2
    if t <= half:
        return ROQSIM_WALL_START + t / 1.25
    return ROQSIM_WALL_START + half / 1.25 + 0.5 + (t - half)


def write_roqsim_mcap(path: Path, samples: int = ROQSIM_SAMPLES, chunk_size: int = 2048,
                      finish: bool = True, meta: dict | None = None) -> Path:
    """A recording in roqsim's shape: its four channels, chunked zstd, its metadata at start
    and again at close, the entity roster changing halfway.

    *chunk_size* is small so the file is many chunks and a reader following it sees the rows
    arrive in batches. With *finish* false the writer is dropped without its footer, as a
    killed run leaves it. *meta* overrides fields of the provenance record (``world``,
    ``overrides``, ``format_version``), in both the opening and the closing copy.
    """
    import struct

    from mcap.writer import CompressionType, Writer

    num, den = ROQSIM_FPS
    meta_start = {**ROQSIM_META, **(meta or {})}
    meta_close = {**ROQSIM_META_CLOSE, **(meta or {})}
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        writer = Writer(fh, chunk_size=chunk_size, compression=CompressionType.ZSTD)
        writer.start(profile="roqsim", library="test")
        writer.add_metadata("roqsim.recording", {"json": json.dumps(meta_start)})
        writer.add_metadata("roqsim.entities",
                            {"json": json.dumps({"entities": ROQSIM_ROSTER_START})})
        state = writer.register_channel("state", "roqsim.state", 0)
        channels = {}
        for name in ("poses", "joints", "clock"):
            sid = writer.register_schema(f"roqsim.{name}", "jsonschema", b'{"type": "object"}')
            channels[name] = writer.register_channel(name, "json", sid)
        for i in range(samples):
            t = i * den / num
            w = roqsim_wall_time(t, samples * den / num)
            log, pub = int(round(t * 1e9)), int(round(w * 1e9))
            if i == samples // 2:
                writer.add_metadata("roqsim.entities",
                                    {"json": json.dumps({"entities": ROQSIM_ROSTER_END})})
            bodies = {b: [k + 0.1 * i, 2.0 * k, 0.0, 0.0, 0.0, 0.0, 1.0,
                          0.1 * num / den, 0.0, 0.0, 0.0, 0.0, 0.0]
                      for k, b in enumerate(ROQSIM_BODIES)}
            writer.add_message(channels["poses"], log,
                               json.dumps({"t": t, "w": w, "bodies": bodies}).encode(), pub, i)
            writer.add_message(channels["joints"], log, json.dumps(
                {"t": t, "w": w, "q": {j: 0.01 * i * (k + 1)
                                        for k, j in enumerate(ROQSIM_JOINTS)}}).encode(), pub, i)
            writer.add_message(channels["clock"], log,
                               json.dumps({"wall_ts": w, "sim_ts": t}).encode(), pub, i)
            writer.add_message(state, log, struct.pack("<dd4f", t, w, 1.0, 2.0, 3.0, 4.0),
                               pub, i)
        if finish:
            writer.add_metadata("roqsim.recording", {"json": json.dumps(meta_close)})
            writer.finish()
    return path


def make_roqsim_campaign(root: Path, runs=(("cfg", 0),), verdict=True, samples=ROQSIM_SAMPLES,
                         finish=True) -> Path:
    """A stepped-run campaign: every run carries roqsim's recording and nothing else -- no
    scenario bag, no job."""
    for config, run_id in runs:
        run = root / config / str(run_id)
        write_roqsim_mcap(run / "roqsim_bag" / "roqsim.mcap", samples=samples, finish=finish)
        if verdict:
            (run / "test.xml").write_text("<testsuite/>")
    (root / "_transient").mkdir(parents=True, exist_ok=True)
    (root / "_transient" / "job_links.yaml").write_text("{}\n")
    return root
