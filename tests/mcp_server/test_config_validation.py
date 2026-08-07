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
    bad.write_text("version: 2\nexecution: {scenario_file: x.osc\n  oops: [unclosed\n")
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
        "version: 2\n"
        "execution:\n"
        "  containers: {scenario: {image: example:latest}}\n"
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
        "version: 2\n"
        "execution:\n"
        "  containers: {scenario: {image: example:latest}}\n"
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
        "version: 2\n"
        "configuration:\n"
        "- name: c1\n"
        "  variations:\n"
        "  - ParameterVariationList:\n"
        "      name: growth_rate\n"
        "      values: [0.1, 0.2, 0.3]\n"
        "execution:\n"
        "  containers: {scenario: {image: example:latest}}\n"
        "  runs: 2\n"
        "  scenario_file: scenario.osc\n"
    )
    report = validate_project_file(str(vast))
    assert report["valid"] is True, report["problems"]
    assert report["problems"] == []
    assert report["configs"] == 3
    assert report["total_trials"] == report["configs"] * report["runs_per_config"]


def test_scene3d_without_a_capture_producing_simulator_is_refused():
    """A scene3d panel replays a run capture, so the configured simulator must make one.

    Asked of the backend rather than pattern-matched out of the campaign's wheel names:
    in the shape where the simulator runs from its own image, a campaign installs no
    simulator packages at all, so the old signal would have found nothing.
    """
    from robovast.common.config_validation import _run_capture_problems
    from robovast.common.simulators import SimulatorBackend
    import robovast.common.simulators as sim_mod

    class _NoCapture(SimulatorBackend):
        pass

    raw = {
        "execution": {"mode": "ros2", "containers": {
            "simulation": {"backend": "nocapture", "stage": "x"}}},
        "visualization": {"panels": [{"scene3d": {}}]},
    }

    original = sim_mod.resolve_backend
    try:
        sim_mod.resolve_backend = lambda name, base_dir="": _NoCapture()
        problems = _run_capture_problems(raw)
        assert len(problems) == 1
        assert "does not produce one" in problems[0]["message"]
        assert problems[0]["field"] == "visualization.panels[0]"

        class _WithCapture(SimulatorBackend):
            def produces_run_capture(self, cfg, execution):
                return True

        sim_mod.resolve_backend = lambda name, base_dir="": _WithCapture()
        assert _run_capture_problems(raw) == []
    finally:
        sim_mod.resolve_backend = original


def test_the_capture_check_stays_quiet_without_a_backend():
    """Nothing here could tell where an unconfigured simulator's capture would come from."""
    from robovast.common.config_validation import _run_capture_problems
    raw = {"execution": {"containers": {"scenario": {"image": "a"}}},
           "visualization": {"panels": [{"scene3d": {}}]}}
    assert _run_capture_problems(raw) == []
