# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""What a campaign's declared plugins actually resolved to.

A ``plugins:`` entry is usually not a pin. ``scenario_mt @ git+https://host/repo@main``
resolves to different code every week, and the only thing recorded before this was a hash of
the *specs* -- identical across every one of those resolutions. So a re-run installed
something else and nothing said so.

Synthetic dist-info directories rather than a real pip install: the point is what gets read
out of the metadata pip writes, and a network install would make the test slow, flaky and
dependent on whatever the remote branch points at today -- which is the very problem here.
"""

import json
import pathlib

from robovast.common.config_plugins import PLUGIN_DIRNAME, resolved_plugin_versions


def _install(vast_dir: pathlib.Path, name: str, version: str, direct_url: dict | None = None):
    """Write the dist-info pip would leave behind for *name* in the workspace plugin dir."""
    dist = vast_dir / PLUGIN_DIRNAME / f"{name.replace('-', '_')}-{version}.dist-info"
    dist.mkdir(parents=True)
    (dist / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
                                   encoding="utf-8")
    if direct_url is not None:
        (dist / "direct_url.json").write_text(json.dumps(direct_url), encoding="utf-8")


def test_a_vcs_spec_records_the_resolved_commit(tmp_path):
    """The whole point: '@main' is not a pin, and the commit it resolved to is."""
    _install(tmp_path, "scenario-mt", "1.4.2", {
        "url": "https://github.com/org/repo",
        "vcs_info": {"vcs": "git", "requested_revision": "main", "commit_id": "b" * 40},
    })
    record = resolved_plugin_versions(str(tmp_path),
                                     ["scenario_mt @ git+https://github.com/org/repo@main"])
    assert record["scenario_mt"]["version"] == "1.4.2"
    assert record["scenario_mt"]["commit"] == "b" * 40
    assert record["scenario_mt"]["url"] == "https://github.com/org/repo"
    # Intent is kept beside outcome, so the record shows that a floating ref was asked for.
    assert record["scenario_mt"]["requested"].endswith("@main")


def test_an_index_install_records_only_the_version(tmp_path):
    """An index install has no direct_url.json, and for one the version *is* a sufficient
    pin -- so absent keys here are correct rather than missing information."""
    _install(tmp_path, "my-plugin", "1.2.3")
    record = resolved_plugin_versions(str(tmp_path), ["my_plugin>=1.0"])
    assert record["my_plugin"] == {"requested": "my_plugin>=1.0", "version": "1.2.3"}


def test_names_match_across_dash_and_underscore(tmp_path):
    """pip normalises distribution names; a spec written either way must still match, or the
    plugin reads as unresolved when it is installed perfectly well."""
    _install(tmp_path, "scenario-mt", "0.1")
    assert resolved_plugin_versions(str(tmp_path), ["scenario_mt"])["scenario_mt"]["version"] == "0.1"
    assert resolved_plugin_versions(str(tmp_path), ["Scenario-MT"])["Scenario-MT"]["version"] == "0.1"


def test_a_plugin_installed_elsewhere_is_recorded_as_unresolved(tmp_path):
    """Declared but not in the workspace dir means pip installed nothing, because it was
    already importable from elsewhere. That is a fact a re-run needs -- the version that ran
    came from somewhere this record cannot name -- so it is recorded, not omitted."""
    record = resolved_plugin_versions(str(tmp_path), ["already_there"])
    assert record["already_there"] == {"requested": "already_there", "resolved": False}


def test_no_plugins_and_no_dir_are_both_empty(tmp_path):
    assert resolved_plugin_versions(str(tmp_path), []) == {}
    assert resolved_plugin_versions(str(tmp_path), None) == {}


def test_a_malformed_direct_url_does_not_fail_the_campaign(tmp_path):
    """This is provenance. Failing to record it must never take down the campaign that was
    about to record it -- the version is still worth having."""
    _install(tmp_path, "broken", "9.9")
    dist = next((tmp_path / PLUGIN_DIRNAME).glob("broken-*.dist-info"))
    (dist / "direct_url.json").write_text("{not json", encoding="utf-8")
    record = resolved_plugin_versions(str(tmp_path), ["broken"])
    assert record["broken"]["version"] == "9.9"
    assert "commit" not in record["broken"]


def test_the_record_round_trips_and_absence_means_unknown(tmp_path):
    """``None`` from the reader must not be confused with "no plugins".

    A campaign recorded before this file existed declared plugins without resolving them
    anywhere, so absence is *unknown*. Conflating it with empty would let a pre-flight report
    a re-run as safely pinned when nothing about its plugins was ever captured.
    """
    from robovast.common.campaign_data import read_plugins_record, write_plugins_record

    campaign = tmp_path / "campaign-2026-01-01-000000"
    _install(tmp_path, "scenario-mt", "1.4.2", {
        "url": "https://github.com/org/repo",
        "vcs_info": {"vcs": "git", "requested_revision": "main", "commit_id": "c" * 40},
    })
    resolved = resolved_plugin_versions(str(tmp_path), ["scenario_mt @ git+.../repo@main"])
    write_plugins_record(campaign, resolved)
    assert read_plugins_record(campaign) == resolved

    # No plugins declared: the record is still WRITTEN, and reads back as {} -- "asked, none".
    # It is the only thing separating that from a campaign predating the file, and since the
    # publication gate treats unknown as opaque, not writing it made every campaign without
    # plugins unpublishable.
    empty = tmp_path / "campaign-2025-01-01-000000"
    write_plugins_record(empty, {})
    assert (empty / "_execution" / "plugins.yaml").exists()
    assert read_plugins_record(empty) == {}

    # Absent is the other answer, and stays unknown.
    never = tmp_path / "campaign-2024-01-01-000000"
    never.mkdir()
    assert read_plugins_record(never) is None
