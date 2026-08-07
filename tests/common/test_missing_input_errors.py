# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A .vast pointing at files that are not there must fail as a clean user error.

The failure used to be ``shutil``'s ``[Errno 2] ... '<path>'`` raised mid-staging:
no ``.vast`` key, no resolution base, and a traceback in the durable failure
record. These guard the reporting, not just the raising.
"""

import os

import pytest

from robovast.common import check_campaign_inputs
from robovast.common.errors import CampaignConfigError
from robovast.common.status import failure_detail


def _campaign_data(tmp_path, *, scenario_exists=True, run_files=()):
    vast = tmp_path / "campaign.vast"
    vast.write_text("version: 2\n")
    scenario = tmp_path / "sub" / "scenario.osc"
    if scenario_exists:
        scenario.parent.mkdir(parents=True, exist_ok=True)
        scenario.write_text("scenario test:\n")
    return {"vast": str(vast), "scenario_file": str(scenario),
            "_run_files": list(run_files)}


def test_missing_scenario_file_names_the_vast_key(tmp_path):
    with pytest.raises(CampaignConfigError) as excinfo:
        check_campaign_inputs(_campaign_data(tmp_path, scenario_exists=False))
    message = str(excinfo.value)
    assert "execution.scenario_file" in message
    assert str(tmp_path / "sub" / "scenario.osc") in message


def test_all_missing_inputs_reported_in_one_error(tmp_path):
    data = _campaign_data(tmp_path, scenario_exists=False,
                          run_files=["files/params.yaml", "files/map.yaml"])
    with pytest.raises(CampaignConfigError) as excinfo:
        check_campaign_inputs(data)
    message = str(excinfo.value)
    assert "files/params.yaml" in message and "files/map.yaml" in message
    # run_files resolve against the .vast's directory, and the message must show
    # that base — it is the difference between "typo" and "wrong working directory".
    assert str(tmp_path / "files" / "params.yaml") in message


def test_run_files_resolved_against_the_vast_directory(tmp_path):
    """A run file present next to the .vast passes regardless of the CWD."""
    data = _campaign_data(tmp_path, run_files=["files/params.yaml"])
    (tmp_path / "files").mkdir()
    (tmp_path / "files" / "params.yaml").write_text("{}\n")
    cwd = os.getcwd()
    os.chdir(tmp_path.parent)
    try:
        check_campaign_inputs(data)
    finally:
        os.chdir(cwd)


def test_failure_record_carries_no_traceback(tmp_path):
    """`failure_detail` must record the message alone for this user error."""
    with pytest.raises(CampaignConfigError) as excinfo:
        check_campaign_inputs(_campaign_data(tmp_path, scenario_exists=False))
    detail = failure_detail(excinfo.value)
    assert "Traceback" not in detail
    assert detail == str(excinfo.value)


def test_generation_fails_before_staging(tmp_path):
    """The check also fires at config generation — the first point the path exists."""
    from robovast.common import generate_scenario_variations
    vast = tmp_path / "campaign.vast"
    vast.write_text(
        "version: 2\n"
        "execution:\n"
        "  containers: {scenario: {image: i}}\n"
        "  image: ghcr.io/cps-test-lab/robovast:latest\n"
        "  scenario_file: sub/scenario.osc\n"
        "  runs: 1\n"
        "configuration:\n"
        "- name: base\n"
    )
    with pytest.raises(CampaignConfigError) as excinfo:
        generate_scenario_variations(str(vast))
    assert "execution.scenario_file" in str(excinfo.value)
