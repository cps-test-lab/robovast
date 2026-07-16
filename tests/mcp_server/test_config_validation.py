# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for collect-all ``.vast`` validation.

Guards the two properties an LLM-facing validator needs: it must never crash the
process (the old ``load_config`` did ``sys.exit(1)`` on a YAML error), and it
must report *all* problems at once with locations.
"""

import shutil
from pathlib import Path

import pytest

from robovast.common.config_validation import validate_project_file

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GROWTH_SIM = _REPO_ROOT / "configs" / "examples" / "growth_sim"


def test_malformed_yaml_returns_problem_without_exiting(tmp_path):
    bad = tmp_path / "bad.vast"
    bad.write_text("version: 1\nexecution: {scenario_file: x.osc\n  oops: [unclosed\n")
    # Must not raise SystemExit / kill the process.
    report = validate_project_file(str(bad))
    assert report["valid"] is False
    assert [p["stage"] for p in report["problems"]] == ["parse"]


def test_missing_file_is_a_problem_not_an_exception(tmp_path):
    report = validate_project_file(str(tmp_path / "nope.vast"))
    assert report["valid"] is False
    assert report["problems"][0]["stage"] == "file"


def test_multiple_errors_collected_with_locations(tmp_path):
    vast = tmp_path / "multi.vast"
    vast.write_text(
        "version: 1\n"
        "execution:\n"
        "  scenario_file: does_not_exist.osc\n"
        "configuration:\n"
        "  - name: c1\n"
        "    variations:\n"
        "      - NoSuchVariationType: {}\n"
    )
    report = validate_project_file(str(vast))
    assert report["valid"] is False
    stages = {p["stage"] for p in report["problems"]}
    # All at once: a missing scenario file AND an unknown variation type (plus
    # any schema problems) — not just the first one.
    assert "scenario_file" in stages
    assert "variation" in stages
    var_problem = next(p for p in report["problems"] if p["stage"] == "variation")
    assert var_problem["config"] == "c1"  # located to the config block


@pytest.mark.skipif(not (_GROWTH_SIM / "growth_sim.vast").exists(),
                    reason="growth_sim example not present")
def test_valid_project_reports_counts(tmp_path):
    # Copy the example so generation caches land in tmp, not the repo tree.
    dst = tmp_path / "growth_sim"
    shutil.copytree(_GROWTH_SIM, dst)
    report = validate_project_file(str(dst / "growth_sim.vast"))
    assert report["valid"] is True, report["problems"]
    assert report["problems"] == []
    assert report["configs"] > 0
    assert report["total_trials"] == report["configs"] * report["runs_per_config"]
