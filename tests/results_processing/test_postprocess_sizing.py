# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""What a campaign may say about how much its postprocessing gets, on either lane.

The figure also decides how many bags convert at once; that half is
``test_conversion_cpu_budget``, which tests the container-side reading it comes from.
"""

import pytest

from robovast.results_processing.postprocessing import (POSTPROCESS_CONVERT_DEFAULTS,
                                                        postprocess_convert_resources)


def _vast(tmp_path, body: str) -> str:
    path = tmp_path / "campaign.vast"
    path.write_text(f"version: 3\nmetadata: {{name: x}}\n{body}", encoding="utf-8")
    return str(path)


def test_a_campaign_that_declares_nothing_gets_the_shared_default(tmp_path):
    path = _vast(tmp_path, "results_processing:\n  postprocessing:\n  - rosbags_tf_to_csv\n")
    assert postprocess_convert_resources(path) == POSTPROCESS_CONVERT_DEFAULTS


def test_a_config_with_no_results_block_at_all_still_resolves(tmp_path):
    """A campaign may be postprocessed without declaring the section -- the infrastructure
    handlers are injected regardless -- so this must answer rather than raise."""
    assert postprocess_convert_resources(
        _vast(tmp_path, "execution:\n  containers: {}\n")) == POSTPROCESS_CONVERT_DEFAULTS


def test_each_resource_defaults_independently(tmp_path):
    """Declaring one must not silently drop the other to nothing.

    Layered over the defaults rather than replacing them: a campaign raising only memory
    would otherwise leave the conversion with no cpu figure, and a zero-cpu container is
    one the scheduler places anywhere and the fan-out reads as a single worker.
    """
    sized = postprocess_convert_resources(
        _vast(tmp_path, "results_processing:\n  resources:\n    memory: 32Gi\n"))
    assert sized == {"cpu": POSTPROCESS_CONVERT_DEFAULTS["cpu"], "memory": "32Gi"}


def test_a_per_cluster_list_needs_a_lane_that_can_choose(tmp_path):
    """The local lane has no cluster context, so there is no entry to pick.

    Refused by name rather than guessed at: taking the first entry would run the
    conversion at another cluster's figure, and taking the default would ignore a
    declaration the campaign made.
    """
    path = _vast(tmp_path, "results_processing:\n  resources:\n    cpu:\n    - ctx-a: 4\n")
    with pytest.raises(ValueError, match="no cluster context"):
        postprocess_convert_resources(path)


def test_a_lane_with_a_context_resolves_the_list(tmp_path):
    path = _vast(tmp_path, "results_processing:\n  resources:\n    cpu:\n    - ctx-a: 4\n")
    assert postprocess_convert_resources(path, resolver=lambda d: {"cpu": 4})["cpu"] == 4


# -- the local lane ---------------------------------------------------------------


def test_the_local_lane_caps_the_container_it_runs(tmp_path, monkeypatch):
    """A conversion on the local lane is held to the same figure the cluster reserves.

    Without the cap the container sees every core of the workstation, so one ``.vast``
    meant two different things depending on which lane ran it -- and the machine's other
    work paid for the difference.
    """
    from robovast.results_processing import postprocessing_plugins as plugins

    campaign = tmp_path / "campaign-x"
    (campaign / "_config").mkdir(parents=True)
    (campaign / "_config" / "x.vast").write_text(
        "version: 3\nmetadata: {name: x}\n"
        "results_processing:\n  resources:\n    cpu: 500m\n    memory: 2Gi\n",
        encoding="utf-8")

    seen = {}

    class _Process:
        stdout = iter(())
        returncode = 0

        def wait(self):
            return 0

    def _popen(cmd, **_kwargs):
        seen["cmd"] = cmd
        return _Process()

    monkeypatch.setattr(plugins.subprocess, "Popen", _popen)

    plugins.RosbagsProcess()(str(campaign), str(campaign / "_config"),
                             plugins=[{"type": "to_csv", "topics": ["/odom"]}])

    cmd = seen["cmd"]
    # Docker's own spelling: a decimal core count, and a byte count rather than "2Gi".
    assert cmd[cmd.index("--cpus") + 1] == "0.5"
    assert cmd[cmd.index("--memory") + 1] == str(2 * 1024 ** 3)
    # The fan-out is NOT passed alongside it: the conversion reads the cap that is actually
    # in force (its cgroup quota), so a second copy of the number could only disagree.
    assert "--workers" not in cmd


def test_the_config_dir_answers_when_the_tree_holds_no_frozen_config(tmp_path):
    """A campaign tree need not hold its own ``.vast``.

    Results are projected into it by a step that may not have run, and the plugin is also
    called against a directory that is no campaign tree at all -- which is why the caller
    passes the config's directory separately. Preferring the frozen copy keeps both lanes
    answering from one file; falling back to this one keeps the knob working where that
    copy does not exist.
    """
    from robovast.results_processing.postprocessing_plugins import _campaign_config_path

    results = tmp_path / "results"
    results.mkdir()
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    (config_dir / "c.vast").write_text("version: 3\n", encoding="utf-8")

    assert _campaign_config_path(str(results), str(config_dir)) == str(config_dir / "c.vast")


def test_no_config_anywhere_is_an_answer_and_not_a_failure(tmp_path):
    """The block is optional, so nothing to read means the defaults. Refusing to convert
    over an optional block nobody wrote would fail campaigns that are entirely correct."""
    from robovast.results_processing.postprocessing_plugins import _campaign_config_path

    assert _campaign_config_path(str(tmp_path), str(tmp_path)) is None
    assert postprocess_convert_resources(None) == POSTPROCESS_CONVERT_DEFAULTS


def test_an_ambiguous_config_dir_is_not_guessed_at(tmp_path):
    """Two candidates and no campaign config to prefer: taking either would size the
    conversion from a file that may describe a different campaign."""
    from robovast.results_processing.postprocessing_plugins import _campaign_config_path

    for name in ("a.vast", "b.vast"):
        (tmp_path / name).write_text("version: 3\n", encoding="utf-8")
    assert _campaign_config_path(str(tmp_path), str(tmp_path)) is None
