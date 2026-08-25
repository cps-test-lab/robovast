# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""The three scene routes, driven through the app the way the panel drives them.

What is pinned here is the *shape* of the interaction, because each part copies an existing pattern and a
drift would be silent: status is pure (a GET that built things would fire on a browser prefetch), starting
is a POST returning ActionResult like ``postprocessing/run``, and the bytes are served from the shared
cache like a panel bundle — including the **sibling** fetch, since the descriptor's loader reads
``scene.bin`` and every texture as relatives of ``scene.json``.
"""

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from robovast.service import scene_cache
from robovast.service.app import build_app
from robovast.service.local_transport import LocalTransport

CAMPAIGN = "demo-2026-08-06-000000"
QUERY = {"config_name": "goal-1", "run_id": "0"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    """An app over a fake results root, with the generator replaced by a local writer.

    Only the *command* is faked: everything else — the generator framework, the atomic swap, the cache
    key, the routes — is the real thing. The container path is covered against a live cluster in
    ``tests/execution/test_aux_container_transfer.py``.
    """
    monkeypatch.setenv("ROBOVAST_SCENE_CACHE", str(tmp_path / "cache"))
    scene_cache._locks.clear()
    # Every test here resolves to the *same* cache key (one fixture world, one image digest), so a
    # recorded failure would otherwise leak into the next test's status.
    scene_cache._failures.clear()

    results = tmp_path / "results"
    run_dir = results / CAMPAIGN / "goal-1" / "0" / "capture"
    run_dir.mkdir(parents=True)
    (results / CAMPAIGN / "_execution").mkdir(parents=True)
    (results / CAMPAIGN / "_execution" / "execution.yaml").write_text(
        "image: build:x\nimage_revision: harbor/x@sha256:" + "a" * 64 + "\n", encoding="utf-8")
    (run_dir / "capture.json").write_text(
        json.dumps({"producer": "roqsim", "world": "pkg:depot", "overrides": {}}), encoding="utf-8")

    fake = tmp_path / "fake.sh"
    fake.write_text('#!/bin/sh\nmkdir -p "$1"\n'
                    'echo \'{"bodies":[]}\' > "$1/scene.json"\nprintf xyz > "$1/scene.bin"\n',
                    encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setattr(scene_cache, "_generate_entry",
                        lambda i, k, m: {"shell": {"out": k, "command": f"{fake} {{out}}"}})
    monkeypatch.setattr(LocalTransport, "_campaigns_root", lambda self: Path(results))
    return TestClient(build_app(LocalTransport()))


def _wait_ready(client, tries=60):
    for _ in range(tries):
        body = client.get(f"/campaigns/{CAMPAIGN}/scene", params=QUERY).json()
        if body["cached"] or body["error"]:
            return body
        time.sleep(0.05)
    raise AssertionError("geometry never became ready")


def test_status_is_pure_then_post_builds_then_assets_resolve(client):
    cold = client.get(f"/campaigns/{CAMPAIGN}/scene", params=QUERY).json()
    assert cold["cached"] is False and cold["generation_required"] is True
    assert cold["world"] == "pkg:depot"
    assert cold["url"] == ""

    # Reading status repeatedly must not build anything: the POST is the only trigger.
    for _ in range(3):
        assert client.get(f"/campaigns/{CAMPAIGN}/scene", params=QUERY).json()["cached"] is False

    started = client.post(f"/campaigns/{CAMPAIGN}/scene/run", params=QUERY).json()
    assert started["ok"] is True

    ready = _wait_ready(client)
    assert ready["cached"] is True and ready["generation_required"] is False
    assert ready["bytes"] > 0
    assert "/scene_assets/" in ready["url"] and ready["url"].endswith("/scene.json")

    asset = client.get(ready["url"])
    assert asset.status_code == 200
    assert json.loads(asset.content) == {"bodies": []}

    # The loader fetches scene.bin as a *sibling* of scene.json, so one URL prefix must address the
    # whole entry -- which is why the cache key is in the path rather than re-derived per file.
    sibling = client.get(ready["url"].rsplit("/", 1)[0] + "/scene.bin")
    assert sibling.status_code == 200 and sibling.content == b"xyz"


def test_a_second_post_joins_rather_than_rebuilding(client):
    client.post(f"/campaigns/{CAMPAIGN}/scene/run", params=QUERY)
    ready = _wait_ready(client)
    assert ready["cached"]
    again = client.post(f"/campaigns/{CAMPAIGN}/scene/run", params=QUERY).json()
    assert again["ok"] is True and "already cached" in again["message"]


def test_assets_cannot_escape_their_entry(client):
    client.post(f"/campaigns/{CAMPAIGN}/scene/run", params=QUERY)
    ready = _wait_ready(client)
    prefix = ready["url"].rsplit("/", 1)[0]
    for bad in ("/../../../etc/passwd", "/nope.png"):
        assert client.get(prefix + bad).status_code == 404


def test_a_run_without_a_capture_reports_a_reason(client):
    body = client.get(f"/campaigns/{CAMPAIGN}/scene",
                      params={"config_name": "goal-9", "run_id": "0"}).json()
    assert body["cached"] is False
    assert "no capture" in body["error"]
    assert body["error"] == body["note"], "the reason is what a viewer shows"


def test_an_unrecorded_override_set_is_flagged_not_assumed(client, tmp_path):
    """A capture predating override recording must not be silently treated as 'no overrides'."""
    manifest = tmp_path / "results" / CAMPAIGN / "goal-1" / "0" / "capture" / "capture.json"
    manifest.write_text(json.dumps({"producer": "roqsim", "world": "pkg:depot"}), encoding="utf-8")
    body = client.get(f"/campaigns/{CAMPAIGN}/scene", params=QUERY).json()
    assert body["overrides_known"] is False
    assert "predates override recording" in body["note"]


def test_the_scene_names_are_reserved_against_plugin_endpoints():
    """A plugin endpoint called `scene` would shadow these routes silently."""
    from robovast.service.endpoint_plugin import RESERVED_CAMPAIGN_ENDPOINTS
    assert {"scene", "scene_assets"} <= RESERVED_CAMPAIGN_ENDPOINTS


def test_a_failed_build_reports_its_reason_instead_of_looking_unbuilt(client, monkeypatch, tmp_path):
    """A failure must reach the panel, not just the service log.

    Generation runs on a background thread, so by the time it fails the POST has already returned its
    ActionResult -- the status endpoint is the only route left. Without this the status falls back to
    "geometry has not been built for this world yet", which is what a *never asked* run looks like, so a
    viewer shows Retry forever while the real reason (observed: `ModuleNotFoundError: rclpy` inside the
    aux container) is visible only to whoever reads the log.
    """
    boom = tmp_path / "boom.sh"
    boom.write_text("#!/bin/sh\necho 'No module named rclpy' >&2\nexit 1\n", encoding="utf-8")
    boom.chmod(0o755)
    monkeypatch.setattr(scene_cache, "_generate_entry",
                        lambda i, k, m: {"shell": {"out": k, "command": f"{boom} {{out}}"}})

    assert client.post(f"/campaigns/{CAMPAIGN}/scene/run", params=QUERY).json()["ok"] is True
    for _ in range(60):
        body = client.get(f"/campaigns/{CAMPAIGN}/scene", params=QUERY).json()
        if body["error"]:
            break
        time.sleep(0.05)
    assert body["cached"] is False
    assert body["in_progress"] is False, "a dead build must not keep the panel polling"
    assert "could not build the scene descriptor" in body["error"]
    assert body["error"] == body["note"], "the reason is what a viewer shows"


def test_a_retry_clears_the_previous_reason(client, monkeypatch, tmp_path):
    """Otherwise a stale error outlives the attempt replacing it, and a good build looks broken."""
    boom = tmp_path / "boom.sh"
    boom.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    boom.chmod(0o755)
    failing = {"shell": {"out": "", "command": f"{boom} {{out}}"}}
    monkeypatch.setattr(scene_cache, "_generate_entry",
                        lambda i, k, m: {"shell": {**failing["shell"], "out": k}})
    client.post(f"/campaigns/{CAMPAIGN}/scene/run", params=QUERY)
    for _ in range(60):
        if client.get(f"/campaigns/{CAMPAIGN}/scene", params=QUERY).json()["error"]:
            break
        time.sleep(0.05)

    good = tmp_path / "good.sh"
    good.write_text('#!/bin/sh\nmkdir -p "$1"\necho \'{"bodies":[]}\' > "$1/scene.json"\n'
                    'printf xyz > "$1/scene.bin"\n', encoding="utf-8")
    good.chmod(0o755)
    monkeypatch.setattr(scene_cache, "_generate_entry",
                        lambda i, k, m: {"shell": {"out": k, "command": f"{good} {{out}}"}})
    client.post(f"/campaigns/{CAMPAIGN}/scene/run", params=QUERY)
    ready = _wait_ready(client)
    assert ready["cached"] is True
    assert ready["error"] == "", "the previous failure outlived the attempt that replaced it"
