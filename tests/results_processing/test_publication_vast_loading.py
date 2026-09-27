# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The publication plugins read a campaign's .vast the way every archived read does.

Through the config loader, so a base named by ``extends`` contributes its ``metadata`` and an
older version is migrated in memory -- and an unreadable file stops the plugin with a named
error instead of publishing as though the file had said nothing.
"""

import pytest

from robovast.common.migrations import SUPPORTED_CONFIG_VERSION
from robovast.results_processing.publication import _execute_plugin
from robovast.results_processing.publication_plugins import zenodo
from robovast.results_processing.publication_plugins import zip as zip_plugin

_BODY = """configuration:
  - name: cfg
    variations: []
execution:
  containers:
    scenario: {}
  runs: 1
  scenario_file: scenario.osc
"""


def _vast_extending_a_base(tmp_path):
    (tmp_path / "base.vast").write_text(
        f"version: {SUPPORTED_CONFIG_VERSION}\nmetadata:\n  robot_id: tb4\n{_BODY}")
    child = tmp_path / "campaign.vast"
    child.write_text(f"version: {SUPPORTED_CONFIG_VERSION}\nextends: base.vast\n")
    return str(child)


def _unreadable_vast(tmp_path):
    bad = tmp_path / "broken.vast"
    bad.write_text("metadata: [unclosed\n")
    return str(bad)


def test_zip_takes_its_filename_values_from_the_merged_config(tmp_path):
    assert zip_plugin._load_vast_metadata(_vast_extending_a_base(tmp_path)) == {"robot_id": "tb4"}


def test_zenodo_reads_the_metadata_a_base_contributes(tmp_path):
    data = zenodo._load_vast_data(_vast_extending_a_base(tmp_path))
    assert data["metadata"] == {"robot_id": "tb4"}


@pytest.mark.parametrize("load", [zip_plugin._load_vast_metadata, zenodo._load_vast_data])
def test_an_unreadable_vast_is_an_error_not_an_empty_config(tmp_path, load):
    with pytest.raises(ValueError, match="broken.vast"):
        load(_unreadable_vast(tmp_path))
    with pytest.raises(FileNotFoundError):
        load(str(tmp_path / "missing.vast"))


def test_a_zip_named_from_an_unreadable_vast_fails_the_plugin(tmp_path):
    results = tmp_path / "results"
    (results / "camp-2026-01-01-000000").mkdir(parents=True)
    ok, message, artifacts = _execute_plugin(
        "zip", zip_plugin.Zip(), {"filename": "data_{robot_id}.zip"}, str(results),
        str(tmp_path), _unreadable_vast(tmp_path))
    assert not ok and "broken.vast" in message and artifacts == []


def test_zenodo_stops_before_touching_the_network_when_the_vast_is_unreadable(
        tmp_path, monkeypatch):
    def no_network(*_args, **_kwargs):
        raise AssertionError("contacted Zenodo despite an unreadable .vast")
    monkeypatch.setenv("ZENODO_ACCESS_TOKEN", "t")
    monkeypatch.setattr(zenodo.requests, "post", no_network)
    monkeypatch.setattr(zenodo.requests, "get", no_network)
    monkeypatch.setattr(zenodo.requests, "put", no_network)
    artifact = tmp_path / "data.zip"
    artifact.write_bytes(b"zip")
    with pytest.raises(ValueError, match="broken.vast"):
        zenodo.Zenodo()(results_dir=str(tmp_path), config_dir=str(tmp_path), record_id=1,
                        overwrite=True, _artifacts=[str(artifact)],
                        _vast_file=_unreadable_vast(tmp_path))
