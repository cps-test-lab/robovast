# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""The floorplan cache is keyed on the digest of the scenery_builder image that builds it."""

import os

import pytest

from robovast_nav.floorplan_generation import generate_floorplan_artifacts


class _Runner:
    """Writes one artifact per ``generate`` and reports a fixed image digest."""

    def __init__(self, workspace, digest):
        self.workspace = str(workspace)
        self.digest = digest
        self.calls = []

    def image_digest(self):
        if self.digest is None:
            raise RuntimeError("no digest")
        return self.digest

    def run(self, command, progress_update_callback=None):
        self.calls.append(command[0])
        if command[0] == "generate":
            out = command[command.index("-o") + 1]
            with open(os.path.join(out, "map.yaml"), "w", encoding="utf-8") as f:
                f.write(f"built by {self.digest}\n")

    def close(self):
        pass


def _build(tmp_path, digest):
    runner = _Runner(tmp_path / "workspace", digest)
    os.makedirs(runner.workspace, exist_ok=True)
    names, image = generate_floorplan_artifacts(
        str(tmp_path / "project"), ["rooms.fpm"], str(tmp_path / "out"),
        lambda _msg: None, runner)
    return runner, names, image


@pytest.fixture(name="project")
def _project(tmp_path):
    (tmp_path / "project").mkdir()
    (tmp_path / "project" / "rooms.fpm").write_text("room a\n")
    return tmp_path


def test_the_same_image_is_served_from_the_cache(project):
    _build(project, "example.org/scenery_builder@sha256:aaaa")
    runner, names, _ = _build(project, "example.org/scenery_builder@sha256:aaaa")
    assert runner.calls == []
    assert names == ["rooms"]


def test_another_image_builds_again(project):
    _build(project, "example.org/scenery_builder@sha256:aaaa")
    runner, _, image = _build(project, "example.org/scenery_builder@sha256:bbbb")
    assert runner.calls == ["transform", "generate"]
    assert image == "example.org/scenery_builder@sha256:bbbb"
    with open(project / "out" / "rooms" / "map.yaml", encoding="utf-8") as f:
        assert f.read() == "built by example.org/scenery_builder@sha256:bbbb\n"


def test_an_unreadable_digest_fails_the_build(project):
    with pytest.raises(RuntimeError, match="no digest"):
        _build(project, None)
