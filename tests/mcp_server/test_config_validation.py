# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for collect-all ``.vast`` validation.

Guards the two properties an LLM-facing validator needs: it must never crash the
process (the old ``load_config`` did ``sys.exit(1)`` on a YAML error), and it
must report *all* problems at once with locations.
"""

from robovast.common.config_validation import (_postprocessing_problems,
                                               validate_project_file)


def test_rosbags_compat_names_validate_clean(tmp_path):
    """rosbags_* names are batched into rosbags_process at runtime, not entry
    points; the validator must accept them just as the runtime does."""
    from robovast.results_processing.postprocessing import ROSBAG_BATCH_NAMES

    entries = list(ROSBAG_BATCH_NAMES) + [{"rosbags_to_csv": {"topics": ["/odom"]}}]
    problems = _postprocessing_problems(entries, str(tmp_path), "rp")
    assert problems == []


def test_compress_and_unknown_postprocessing(tmp_path):
    """compress is a registered entry point (accepted); a bogus name is rejected."""
    problems = _postprocessing_problems(
        ["compress", "definitely_not_a_plugin"], str(tmp_path), "rp")
    # Only the bogus name (index 1) is a problem; compress (index 0) resolved.
    assert len(problems) == 1
    assert problems[0]["field"] == "rp[1]"
    assert "definitely_not_a_plugin" in problems[0]["message"]


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


def test_local_plugin_refs_are_interface_checked(tmp_path):
    """postprocessing / search strategy / extractor local refs are validated."""
    (tmp_path / "plugins").mkdir()
    (tmp_path / "plugins" / "bad_post.py").write_text("class NotAPlugin:\n    pass\n")
    (tmp_path / "plugins" / "bad_strategy.py").write_text("class BadStrategy:\n    pass\n")
    (tmp_path / "plugins" / "bad_extract.py").write_text(
        "from robovast.search.extractor import Extractor\n"
        "class BadExtract(Extractor):\n    pass\n")
    (tmp_path / "scenario.osc").write_text("")

    vast = tmp_path / "broken.vast"
    vast.write_text(
        "version: 1\n"
        "execution:\n"
        "  scenario_file: scenario.osc\n"
        "search:\n"
        "  strategy: plugins/bad_strategy.py:BadStrategy\n"
        "  search_space:\n"
        "    wind: {type: float, low: 0.0, high: 5.0}\n"
        "  objectives:\n"
        "  - name: err\n"
        "    direction: minimize\n"
        "  budget:\n"
        "  - max_generations: 2\n"
        "  per_batch: 2\n"
        "  postprocessing:\n"
        "  - plugins/bad_post.py:NotAPlugin\n"
        "  - plugins/does_not_exist.py:Ghost\n"
        "  extract:\n"
        "    plugin: plugins/bad_extract.py:BadExtract\n"
        "results_processing:\n"
        "  postprocessing:\n"
        "  - plugins/bad_post.py:NotAPlugin\n"
    )
    report = validate_project_file(str(vast))
    assert report["valid"] is False
    stages = {p["stage"] for p in report["problems"]}
    assert {"postprocessing", "search-strategy", "search-extractor"} <= stages
    # Each problem is located to the offending field.
    fields = {p["field"] for p in report["problems"]}
    assert "search.strategy" in fields
    assert "search.extract.plugin" in fields
    assert any(f and f.startswith("results_processing.postprocessing") for f in fields)


def test_valid_project_reports_counts(tmp_path):
    """A self-contained valid project validates clean and reports its counts.

    Built in ``tmp_path`` rather than pointing at a repo example, so the test is
    not coupled to any fixture on disk. The three-value variation list yields
    three configs.
    """
    (tmp_path / "scenario.osc").write_text("scenario test:\n    timeout(10s)\n")
    vast = tmp_path / "valid.vast"
    vast.write_text(
        "version: 1\n"
        "configuration:\n"
        "- name: c1\n"
        "  variations:\n"
        "  - ParameterVariationList:\n"
        "      name: growth_rate\n"
        "      values: [0.1, 0.2, 0.3]\n"
        "execution:\n"
        "  image: example:latest\n"
        "  runs: 2\n"
        "  scenario_file: scenario.osc\n"
    )
    report = validate_project_file(str(vast))
    assert report["valid"] is True, report["problems"]
    assert report["problems"] == []
    assert report["configs"] == 3
    assert report["total_trials"] == report["configs"] * report["runs_per_config"]


def test_scene3d_without_recording_is_refused(tmp_path):
    """A scene3d panel replays a run capture, so the runs must be asked to record one.

    The same failure the campaign-scope descriptor check exists for, one artifact over: nothing
    otherwise declares the dependency, so the campaign runs, passes, and shows a motionless world
    whenever someone finally opens it.
    """
    from robovast.common.config_validation import _run_capture_problems

    raw = {
        "execution": {
            "simulation": "rst.scenario_adapter:MujocoSim",
            "env": [{"ROBOSITO_WORLD": "/config/files/depot.yaml"}],
        },
        "visualization": {"panels": [{"scene3d": {"scene": {"path": "scene/scene.json"}}}]},
    }
    problems = _run_capture_problems(raw)
    assert len(problems) == 1
    assert "ROBOSITO_RECORD" in problems[0]["message"]
    assert "ROBOSITO_CAPTURE_EXPORT_DIR" in problems[0]["message"]
    assert problems[0]["field"] == "visualization.panels[0]"

    # With both set, it is clean -- and a plain mapping is accepted as well as the list form.
    raw["execution"]["env"].extend(
        [{"ROBOSITO_RECORD": "run.npz"}, {"ROBOSITO_CAPTURE_EXPORT_DIR": "capture"}])
    assert _run_capture_problems(raw) == []
    raw["execution"]["env"] = {
        "ROBOSITO_RECORD": "run.npz", "ROBOSITO_CAPTURE_EXPORT_DIR": "capture"}
    assert _run_capture_problems(raw) == []


def test_scene3d_recording_check_only_applies_to_rst():
    """Another simulator producing the same format is not second-guessed by rst's variable names."""
    from robovast.common.config_validation import _run_capture_problems

    raw = {
        "execution": {"simulation": "some_other.adapter:Sim", "env": []},
        "visualization": {"panels": [{"scene3d": {}}]},
    }
    assert _run_capture_problems(raw) == []


def test_panels_without_scene3d_need_no_recording():
    """gazebo.vast's four panels declare no scene3d, so the check must leave it alone."""
    from robovast.common.config_validation import _run_capture_problems

    raw = {
        "execution": {"simulation": "rst.scenario_adapter:MujocoSim", "env": []},
        "visualization": {"panels": ["playback", {"costmap": {"layers": {}}}]},
    }
    assert _run_capture_problems(raw) == []


def test_recording_check_fires_for_a_launch_driven_rst_campaign():
    """rst_basic_nav declares no `simulation` -- it starts the simulator from a ROS launch file.

    Keying only on `execution.simulation` would have left exactly the campaign this feature was built
    for unprotected, so the wheels a campaign installs count as evidence too.
    """
    from robovast.common.config_validation import _run_capture_problems, _uses_rst

    raw = {
        "build": {"python_packages": [
            "mujoco>=3.0",
            ["wheels/rst-0.1.0-py3-none-any.whl", "wheels/rst_mobile-0.1.0-py3-none-any.whl"],
        ]},
        "execution": {"env": [{"PYTHONUNBUFFERED": "1"}]},
        "visualization": {"panels": [{"scene3d": {}}]},
    }
    assert _uses_rst(raw)
    assert len(_run_capture_problems(raw)) == 1

    # A campaign with no rst anywhere is left alone -- its capture may come from somewhere else.
    other = {
        "build": {"python_packages": ["numpy", "some_other_sim"]},
        "execution": {"env": []},
        "visualization": {"panels": [{"scene3d": {}}]},
    }
    assert not _uses_rst(other)
    assert _run_capture_problems(other) == []

    # And the word boundary holds: "burst_sim" is not rst.
    assert not _uses_rst({"build": {"python_packages": ["burst_sim", "worst-case-tools"]}})
