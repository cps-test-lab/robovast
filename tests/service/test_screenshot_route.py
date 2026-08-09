# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Re-rendering one moment of a run, driven through the app the way the MCP tool drives it.

What is pinned here is the *shape*, because each part copies an existing pattern and a drift
would be silent: a POST because it runs the simulator, **synchronous** because a screenshot is
keyed on a camera and a moment and so is never a cache hit, and every refusal a 400 carrying
the reason — a simulator that does not render is a statement about the request, not a 500.

Only the *command* is faked, as in ``test_scene_routes``: the generator framework, the identity
resolution, the cleanup and the routes are the real thing.
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from robovast.common import simulators
from robovast.service import screenshot
from robovast.service.app import build_app
from robovast.service.local_transport import LocalTransport

CAMPAIGN = "demo-2026-08-09-000000"
QUERY = {"config_name": "hexagon-1", "run_id": "0"}

# A 1x1 PNG, so the response is a real image rather than bytes that merely have the name.
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082")


@pytest.fixture(name="env")
def _env(tmp_path, monkeypatch):
    results = tmp_path / "results"
    run = results / CAMPAIGN / "hexagon-1" / "0"
    (run / "capture").mkdir(parents=True)
    (results / CAMPAIGN / "_execution").mkdir(parents=True)
    (results / CAMPAIGN / "_execution" / "execution.yaml").write_text(
        "image: build:x\nimage_revision: harbor/x@sha256:" + "a" * 64 + "\n", encoding="utf-8")
    (run / "capture" / "capture.json").write_text(
        json.dumps({"producer": "rst", "world": "pkg:hexagon", "overrides": {}}),
        encoding="utf-8")
    (run / "run.npz").write_bytes(b"not really a recording")

    monkeypatch.setattr(LocalTransport, "_campaigns_root", lambda self: Path(results))
    monkeypatch.setattr(simulators, "run_state_filename", lambda execution, base_dir="": "run.npz")

    # Stands in for the backend's `rst render`: writes the frame the real one would, and logs
    # its arguments *outside* the render directory — that directory is deleted once the
    # response is sent, which is the behaviour the next test pins.
    writer = tmp_path / "render.sh"
    writer.write_text('#!/bin/sh\nmkdir -p "$(dirname "$2")"\n'
                      'printf %s "$3" > "$5"\ncat "$4" > "$2"\n', encoding="utf-8")
    writer.chmod(0o755)
    png_src = tmp_path / "frame-src.png"
    png_src.write_bytes(PNG)
    args_log = tmp_path / "render.args"

    def command(execution, *, state, at, view, focus, camera, size, base_dir=""):
        del execution, base_dir
        args = f"state={state} at={at} view={sorted((view or {}).items())} " \
               f"focus={focus} camera={camera} size={size}"
        return f'{writer} {state} {{out}}/frame.png "{args}" {png_src} {args_log}'

    monkeypatch.setattr(simulators, "simulation_screenshot_command", command)

    # Run the command on this host instead of in the campaign's image: `image` is what sends
    # the generator to `docker run`, and pulling a fixture digest is not what is under test.
    # The container path is covered against a live cluster in test_aux_container_transfer.
    real_entry = screenshot._entry  # noqa: SLF001 - pinning this module's own seam

    def local_entry(identity, out_name, cmd, state_path):
        entry = real_entry(identity, out_name, cmd, state_path)
        entry["shell"].pop("image")
        return entry

    monkeypatch.setattr(screenshot, "_entry", local_entry)
    with TestClient(build_app(LocalTransport())) as client:
        yield client, tmp_path


def _post(client, **extra):
    return client.post(f"/campaigns/{CAMPAIGN}/screenshot", params={**QUERY, **extra})


def test_a_screenshot_comes_back_as_an_image(env):
    """The bytes the simulator wrote, with the arguments the caller asked for."""
    client, tmp_path = env
    resp = _post(client, at=12.5, view=["azimuth=90", "lookat=1,2,0"], size="640x480")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "image/png"
    assert resp.content == PNG

    # The camera vocabulary has to survive the whole path — route -> parse_view -> backend.
    args = (tmp_path / "render.args").read_text()
    assert "at=12.5" in args and "size=640x480" in args
    assert "('azimuth', '90')" in args and "('lookat', '1,2,0')" in args
    # The recording reached the command as its input, not as a literal placeholder.
    assert "state=" in args and "{inputs[0]}" not in args and "run.npz" in args


def test_the_render_directory_does_not_survive_the_response(env):
    """Nothing here is cacheable, so nothing here is kept.

    The cleanup runs as a background task *after* the body is sent, which is why it is worth a
    test: a leak would be invisible until a service filled its disk with frames nobody can
    name.
    """
    client, _ = env
    assert _post(client).status_code == 200
    leftover = list(Path("/tmp").glob("robovast-screenshot-*"))  # noqa: S108 - mkdtemp's root
    assert not leftover, leftover


def test_an_unknown_view_key_names_the_valid_ones(env):
    """The tool's caller is usually a model, and 'unknown key' without the vocabulary is a
    guess-again."""
    client, _ = env
    resp = _post(client, view=["zoom=3"])
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "zoom" in detail
    for key in ("azimuth", "distance", "elevation", "lookat"):
        assert key in detail


def test_a_fixed_camera_refuses_a_free_camera_s_angle(env):
    """A world-defined camera owns its pose, so this is a contradiction, not a preference —
    and it is caught before an image is pulled rather than by argv inside the container."""
    client, _ = env
    resp = _post(client, camera="overhead", view=["azimuth=90"])
    assert resp.status_code == 400
    assert "overhead" in resp.json()["detail"]


def test_a_run_with_no_recording_says_what_is_missing(env):
    """A run killed by its deadline leaves no state, and 'no moment to render' is the answer —
    not a traceback from a simulator handed a path that is not there."""
    client, tmp_path = env
    (tmp_path / "results" / CAMPAIGN / "hexagon-1" / "0" / "run.npz").unlink()
    resp = _post(client)
    assert resp.status_code == 400
    assert "clean stop" in resp.json()["detail"]


def test_a_simulator_that_cannot_render_says_so(env, monkeypatch):
    """Gazebo's answer, and the same shape as its answer for scene export: a capability this
    campaign's simulator lacks, reported as such."""
    client, _ = env
    monkeypatch.setattr(simulators, "simulation_screenshot_command",
                        lambda *a, **k: None)
    resp = _post(client)
    assert resp.status_code == 400
    assert "renders no views" in resp.json()["detail"]
