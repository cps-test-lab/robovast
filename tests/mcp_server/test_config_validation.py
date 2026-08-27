# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for collect-all ``.vast`` validation.

Guards the two properties an LLM-facing validator needs: it must never crash the
process (the old ``load_config`` did ``sys.exit(1)`` on a YAML error), and it
must report *all* problems at once with locations.
"""

from robovast.common.config_validation import _postprocessing_problems, validate_project_file


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
    bad.write_text("version: 3\nexecution: {scenario_file: x.osc\n  oops: [unclosed\n")
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
        "version: 3\n"
        "execution:\n"
        "  containers: {scenario: {image: 'family:robovast'}}\n"
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
        "version: 3\n"
        "execution:\n"
        "  containers: {scenario: {image: 'family:robovast'}}\n"
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
        "version: 3\n"
        "configuration:\n"
        "- name: c1\n"
        "  variations:\n"
        "  - ParameterVariationList:\n"
        "      scenario: growth_rate\n"
        "      values: [0.1, 0.2, 0.3]\n"
        "execution:\n"
        "  containers: {scenario: {image: 'family:robovast'}}\n"
        "  runs: 2\n"
        # Declared so this stays a project with *nothing* to say about it: without a
        # per-run budget it would carry the liveness advisory, and `problems == []`
        # below is the assertion that a fully-declared project is advised about nothing.
        "  timeout: 300\n"
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
    import robovast.common.simulators as sim_mod
    from robovast.common.config_validation import _run_capture_problems
    from robovast.common.simulators import SimulatorBackend

    class _NoCapture(SimulatorBackend):
        pass

    raw = {
        "execution": {"mode": "ros2", "containers": {
            "simulation": {"backend": "nocapture", "stage": "x"}}},
        "visualization": {"results": {"run_view": {"panels": [{"scene3d": {}}]}}},
    }

    original = sim_mod.resolve_backend
    try:
        sim_mod.resolve_backend = lambda name, base_dir="": _NoCapture()
        problems = _run_capture_problems(raw)
        assert len(problems) == 1
        assert "does not produce one" in problems[0]["message"]
        assert problems[0]["field"] == "visualization.results.run_view.panels[0]"

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
           "visualization": {"results": {"run_view": {"panels": [{"scene3d": {}}]}}}}
    assert _run_capture_problems(raw) == []


# ---------------------------------------------------------------------------
# Build-context advisory
#
# The context is copied, uploaded and mirrored back down once per built container on every
# build, and BuildKit's output never names it — so a project that grows one by accident
# just gets slow builds with no reason given. Pre-flight is the last point where the cost
# is still avoidable, so it is reported here — as an advisory, since a project may
# legitimately be large and this cannot tell the difference.
# ---------------------------------------------------------------------------

def _fat(path, mb):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * (mb * 1024 * 1024))


def test_a_large_build_context_is_advised_but_still_valid(tmp_path):
    from robovast.common.config_validation import _build_context_advisories
    _fat(tmp_path / "bags" / "big.bag", 60)
    problems = _build_context_advisories(tmp_path / "x.vast")
    assert len(problems) == 1
    assert problems[0]["stage"] == "build-context"
    assert "bags" in problems[0]["message"]


def test_a_small_project_is_not_advised(tmp_path):
    from robovast.common.config_validation import _build_context_advisories
    _fat(tmp_path / "plugins" / "pkg.whl", 1)
    assert _build_context_advisories(tmp_path / "x.vast") == []


def test_weight_inside_a_campaign_directory_is_not_advised(tmp_path):
    """It is already excluded from the context, so advising on it would be a false alarm."""
    from robovast.common.config_validation import _build_context_advisories
    (tmp_path / "camp-1" / "_execution").mkdir(parents=True)
    _fat(tmp_path / "camp-1" / "run.bag", 60)
    assert _build_context_advisories(tmp_path / "x.vast") == []


def test_results_are_not_advised(tmp_path):
    """`results/` is ignored wholesale; a downloaded campaign there is not a context cost."""
    from robovast.common.config_validation import _build_context_advisories
    _fat(tmp_path / "results" / "camp-1" / "run.bag", 60)
    assert _build_context_advisories(tmp_path / "x.vast") == []


# --- cpu without memory ------------------------------------------------------------------
#
# A campaign that sized its CPU and not its memory reads as one where the sizing was done.
# The half that is missing is the half that bites: with no memory limit, AVAILABLE_MEM
# reports the node's memory as the run's budget, and the pod's shared /dev/shm is sized
# from the node too -- and overrunning shared memory kills a container with SIGBUS
# (exit 135), not a clean OOM, so it arrives with no reason attached.

def _vast(tmp_path, name, containers):
    path = tmp_path / f"{name}.vast"
    path.write_text("version: 3\nexecution:\n  containers:\n" + containers)
    return str(path)


def test_cpu_without_memory_is_advised(tmp_path):
    from robovast.common.config_validation import _resource_advisories

    path = _vast(tmp_path, "bare", "    sut:\n      resources:\n        cpu: 3.25\n")
    advisory, = _resource_advisories(path)
    assert advisory["stage"] == "resources"
    assert "execution.containers.sut" in advisory["message"]
    assert "AVAILABLE_MEM" in advisory["message"]
    # Not /dev/shm any more: the pool is sized by execution.shm_size, which now has a
    # default, so it no longer follows the memory limits and this advisory no longer
    # speaks for it.
    assert "shm" not in advisory["message"]


def test_cpu_and_memory_together_are_not_advised(tmp_path):
    from robovast.common.config_validation import _resource_advisories

    path = _vast(tmp_path, "sized",
                 "    sut:\n      resources:\n        cpu: 3.25\n        memory: 4Gi\n")
    assert _resource_advisories(path) == []


def test_declaring_neither_is_not_advised(tmp_path):
    """The deliberate asymmetry: an unconstrained container is the default a quick local
    run legitimately uses, and warning about it would be noise on every example here."""
    from robovast.common.config_validation import _resource_advisories

    path = _vast(tmp_path, "unset", "    sut:\n      image: an-image\n")
    assert _resource_advisories(path) == []


def test_every_bare_container_is_named(tmp_path):
    """One advisory, naming all of them: three separate warnings for one mistake reads as
    three mistakes."""
    from robovast.common.config_validation import _resource_advisories

    path = _vast(tmp_path, "three",
                 "    scenario:\n      resources:\n        cpu: 1.25\n"
                 "    sut:\n      resources:\n        cpu: 3.25\n"
                 "    simulation:\n      resources:\n        cpu: 0.75\n        memory: 2Gi\n")
    advisory, = _resource_advisories(path)
    assert "execution.containers.scenario" in advisory["message"]
    assert "execution.containers.sut" in advisory["message"]
    assert "execution.containers.simulation" not in advisory["message"]


def test_an_advisory_never_makes_a_project_invalid(tmp_path):
    """It rides the composition-report path, where `valid` is already True. Adding it to
    validate_project_file's own problems list would have flipped the verdict instead."""
    from robovast.common.config_validation import _resource_advisories

    path = _vast(tmp_path, "adv", "    sut:\n      resources:\n        cpu: 3.25\n")
    for problem in _resource_advisories(path):
        assert problem["stage"] == "resources"
        assert "message" in problem


# --- no declared per-run budget -----------------------------------------------------------
#
# A campaign without `execution.timeout` runs perfectly well. What it cannot do is be
# judged: `stalled` is asserted only against a *declared* budget, so the verdict stays
# null forever and a wedged run and a slow one are the same picture -- with nothing for
# `vast campaign wait` to exit 4 on. Said once here, before compute, rather than shown on every
# poll for the life of the campaign, by when the fix costs a re-run.

def test_a_missing_execution_timeout_is_advised(tmp_path):
    from robovast.common.config_validation import _liveness_advisories

    path = tmp_path / "untimed.vast"
    path.write_text("version: 3\nexecution:\n  runs: 3\n")
    advisory, = _liveness_advisories(str(path))
    assert advisory["stage"] == "liveness"
    assert advisory["field"] == "execution.timeout"
    assert "vast campaign wait" in advisory["message"]


def test_a_declared_timeout_is_not_advised(tmp_path):
    from robovast.common.config_validation import _liveness_advisories

    path = tmp_path / "timed.vast"
    path.write_text("version: 3\nexecution:\n  timeout: 300\n")
    assert _liveness_advisories(str(path)) == []


def test_a_project_with_no_execution_block_is_still_advised(tmp_path):
    """Declaring nothing is not the deliberate default it is for resources: an absent
    budget is exactly as unjudgeable as an absent timeout inside a present block."""
    from robovast.common.config_validation import _liveness_advisories

    path = tmp_path / "bare.vast"
    path.write_text("version: 3\nscenario: x.osc\n")
    assert len(_liveness_advisories(str(path))) == 1
