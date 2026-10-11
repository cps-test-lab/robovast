# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The collect-all validator reads a configuration in the shape the schema accepts."""

from robovast.common.config_validation import (_config_block_problems,
                                               _python_packages_problems, validate_project_file)


def test_an_unknown_scenario_parameter_is_reported(tmp_path):
    block = {"name": "c1", "parameters": {"scenario": {"known": 1, "typo": 2}}}

    problems = _config_block_problems(block, str(tmp_path), ["known"])

    assert [(p["stage"], p["field"]) for p in problems] == [("parameters", "parameters.scenario")]
    assert "typo" in problems[0]["message"]


def test_known_scenario_parameters_are_not_reported(tmp_path):
    block = {"name": "c1", "parameters": {"scenario": {"known": 1}, "sim": {"x": 1}}}

    assert _config_block_problems(block, str(tmp_path), ["known"]) == []


def test_a_missing_container_wheel_is_reported(tmp_path):
    (tmp_path / "present.whl").write_text("")
    raw = {"execution": {"containers": {"sut": {"python_packages": [
        "./present.whl", ["./absent.whl", "numpy==2.0"]]}}}}

    problems = _python_packages_problems(raw, str(tmp_path))

    assert [(p["field"], p["config"]) for p in problems] == [
        ("execution.containers.sut.python_packages", "sut")]
    assert "./absent.whl" in problems[0]["message"]


def test_the_project_report_carries_both(tmp_path):
    (tmp_path / "scenario.osc").write_text("scenario test:\n    timeout(10s)\n")
    vast = tmp_path / "p.vast"
    vast.write_text(
        "version: 7\n"
        "configuration:\n"
        "- name: c1\n"
        "  parameters:\n"
        "    scenario: {not_declared: 1}\n"
        "execution:\n"
        "  containers:\n"
        "    scenario: {image: 'family:robovast', python_packages: ['./missing.whl']}\n"
        "  runs: 1\n"
        "  timeout: 60\n"
        "  scenario_file: scenario.osc\n")

    report = validate_project_file(str(vast))

    assert report["valid"] is False
    stages = {p["stage"] for p in report["problems"]}
    assert {"parameters", "build"} <= stages
