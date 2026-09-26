# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A composed campaign is cached under the image family its refs were resolved against."""

from robovast.common.config_generation import _build_generate_cache_key


def _key(tmp_path, **images):
    """The key of one unchanged ``.vast``: written once, since the key reads a file's mtime."""
    vast = tmp_path / "c.vast"
    if not vast.exists():
        vast.write_text("version: 6\n")
    return _build_generate_cache_key(
        variation_file=str(vast), vast_dir=str(tmp_path), scenario_file="", run_files=[],
        analysis_files=[], configurations=[], **images).fingerprint()


def test_a_deployment_moved_to_another_project_composes_afresh(tmp_path, monkeypatch):
    """The failure this guards: a service whose ROBOVAST_PROJECT changed reused the entry
    composed before, and ran the old project's images for every family ref it had cached."""
    monkeypatch.setenv("ROBOVAST_PROJECT", "registry.example.com/one")
    monkeypatch.setenv("ROBOVAST_PROJECT_TAG", "a")
    before = _key(tmp_path)
    monkeypatch.setenv("ROBOVAST_PROJECT", "registry.example.com/two")
    assert _key(tmp_path) != before
    monkeypatch.setenv("ROBOVAST_PROJECT", "registry.example.com/one")
    monkeypatch.setenv("ROBOVAST_PROJECT_TAG", "b")
    assert _key(tmp_path) != before


def test_a_campaign_naming_the_environment_s_family_shares_its_entry(tmp_path, monkeypatch):
    """The key is what the refs resolve to, not who named it: the same family, the same key."""
    monkeypatch.setenv("ROBOVAST_PROJECT", "registry.example.com/one")
    monkeypatch.setenv("ROBOVAST_PROJECT_TAG", "a")
    assert _key(tmp_path) == _key(tmp_path, image_project="registry.example.com/one",
                                  image_project_tag="a")
    assert _key(tmp_path) != _key(tmp_path, image_project="registry.example.com/two")
