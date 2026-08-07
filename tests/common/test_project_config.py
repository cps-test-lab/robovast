# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Resolving a ``.vast`` without requiring a project.

``ProjectConfig.load`` never checks that the ``.vast`` its record names still exists,
and the record itself is found by walking *up* to the filesystem root — so the resolver
must not hand a caller a path that is not there.
"""

import json

import click

from robovast.common.cli.project_config import resolve_vast_file

_VAST = "version: 2\nexecution:\n  image: i\n  runs: 1\n"


def _write_project(directory, config_name, write_config=True):
    if write_config:
        (directory / config_name).write_text(_VAST, encoding="utf-8")
    (directory / ".robovast_project").write_text(json.dumps(
        {"config": config_name, "results_dir": "results"}), encoding="utf-8")


def test_no_project_no_override(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert resolve_vast_file() is None


def test_project_config_is_used(tmp_path, monkeypatch):
    _write_project(tmp_path, "campaign.vast")
    monkeypatch.chdir(tmp_path)
    assert resolve_vast_file() == str(tmp_path / "campaign.vast")


def test_project_found_by_walking_up(tmp_path, monkeypatch):
    """Pinned deliberately: a parent's project applies to a nested CWD."""
    _write_project(tmp_path, "campaign.vast")
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    monkeypatch.chdir(deep)
    assert resolve_vast_file() == str(tmp_path / "campaign.vast")


def test_stale_project_pointer_is_ignored_with_a_warning(tmp_path, monkeypatch, caplog):
    """A record naming a moved/deleted ``.vast`` must not yield a nonexistent path."""
    _write_project(tmp_path, "gone.vast", write_config=False)
    monkeypatch.chdir(tmp_path)
    with caplog.at_level("WARNING"):
        assert resolve_vast_file() is None
    assert "does not exist" in caplog.text
    assert ".robovast_project" in caplog.text


def test_override_wins_over_project(tmp_path, monkeypatch):
    _write_project(tmp_path, "campaign.vast")
    other = tmp_path / "other.vast"
    other.write_text(_VAST, encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    resolved = {}

    @click.command()
    @click.pass_context
    def _cmd(ctx):
        ctx.ensure_object(dict)
        ctx.obj['vast_file'] = str(other)
        resolved['path'] = resolve_vast_file()

    _cmd.main([], standalone_mode=False)
    assert resolved['path'] == str(other)
