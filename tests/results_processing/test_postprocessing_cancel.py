# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Stopping a campaign while it postprocesses, and what that must leave behind.

A stop is taken between steps: after each of the campaign's own steps, before the tables
are built and before the provenance record is written. So a cancelled campaign keeps its
records, never claims derived data it does not have, and re-running builds the rest.
"""

from robovast.common.campaign_data import campaign_has_derived_data
from robovast.results_processing import postprocessing
from robovast.results_processing.postprocessing import POSTPROCESSING_CANCELLED

from .conftest import write_campaign_db


def _campaign_tree(tmp_path):
    """The smallest campaign ``run_postprocessing`` will finish."""
    root = tmp_path / "camp-2026-01-01-000000"
    write_campaign_db(root, root.name)
    run_dir = root / "cfg-a" / "0"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "nav_metrics.csv").write_text("duration_s,collided\n12.5,0\n")
    (root / "_config").mkdir()
    (root / "_config" / "campaign.vast").write_text(
        "version: 5\nexecution:\n  containers: {}\n"
        "results_processing:\n  postprocessing: []\n")
    return root


def _count_builds(monkeypatch):
    builds = []
    real = postprocessing.build_tables

    def build(*args, **kwargs):
        builds.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(postprocessing, "build_tables", build)
    return builds


def test_a_cancelled_run_builds_nothing(tmp_path, monkeypatch):
    root = _campaign_tree(tmp_path)
    builds = _count_builds(monkeypatch)

    ok, message = postprocessing.run_postprocessing(
        str(tmp_path), campaign=root.name, should_stop=lambda: True)

    assert (ok, message) == (False, POSTPROCESSING_CANCELLED)
    assert builds == []


def test_a_cancelled_run_does_not_claim_the_campaign_is_postprocessed(tmp_path):
    """The provenance record is the evidence a campaign carries derived data.

    Written last, precisely so its presence means every step succeeded -- so a cancelled
    run must stop before it. Otherwise the campaign would read as postprocessed, its
    archive would be named as such, and nobody would re-run the step that never finished.
    """
    root = _campaign_tree(tmp_path)

    postprocessing.run_postprocessing(str(tmp_path), campaign=root.name,
                                      should_stop=lambda: True)

    assert campaign_has_derived_data(str(root)) is False


def test_a_stop_arriving_mid_run_is_taken_at_the_next_step_boundary(tmp_path, monkeypatch):
    """A stop during the table build ends the run before the record, not at the end."""
    root = _campaign_tree(tmp_path)
    builds = _count_builds(monkeypatch)
    calls = {"n": 0}

    def should_stop():
        calls["n"] += 1
        return calls["n"] > 1          # lets the build start, then stops

    ok, message = postprocessing.run_postprocessing(
        str(tmp_path), campaign=root.name, should_stop=should_stop)

    assert (ok, message) == (False, POSTPROCESSING_CANCELLED)
    assert len(builds) == 1            # it did not give up before doing anything
    assert campaign_has_derived_data(str(root)) is False


def test_no_predicate_leaves_the_pipeline_exactly_as_it_was(tmp_path, monkeypatch):
    """Without a predicate nothing is polled and nothing is skipped.

    The re-run entry points postprocess campaigns nothing is driving, so ``None`` has to
    mean "run it all", not "cancel immediately".
    """
    root = _campaign_tree(tmp_path)
    builds = _count_builds(monkeypatch)

    ok, message = postprocessing.run_postprocessing(str(tmp_path), campaign=root.name,
                                                    skip_metadata=True)

    assert ok, message
    assert len(builds) == 1
    assert campaign_has_derived_data(str(root)) is True


# -- the predicate reaches only the plugins that can honour it ---------------

def test_a_plugin_that_cannot_be_cancelled_is_called_exactly_as_before():
    """Plugins are user-supplied callables, including ones written before this existed.

    Passing a keyword such a plugin never declared would fail its step with an argument
    error over a feature it was not asked to have -- so the step would break for everyone
    the moment a campaign carried a stop predicate.
    """
    seen = {}

    def plugin(results_dir, config_dir):
        seen.update(results_dir=results_dir, config_dir=config_dir)
        return True, "done"

    ok, message, _ = postprocessing.execute_postprocessing_plugin(
        plugin_name="old", plugin_func=plugin, params={},
        results_dir="/r", config_dir="/c", should_stop=lambda: False)

    assert (ok, message) == (True, "done")
    assert seen == {"results_dir": "/r", "config_dir": "/c"}


def test_a_plugin_that_declares_the_predicate_receives_it():
    got = {}

    def plugin(results_dir, config_dir, should_stop=None):
        got["should_stop"] = should_stop
        return True, "done"

    def predicate():
        return True

    postprocessing.execute_postprocessing_plugin(
        plugin_name="new", plugin_func=plugin, params={},
        results_dir="/r", config_dir="/c", should_stop=predicate)

    assert got["should_stop"] is predicate


def test_a_plugin_absorbing_keywords_receives_it_too():
    """``**kwargs`` is how most plugins here are written, and it accepts the keyword."""
    got = {}

    def plugin(results_dir, config_dir, **kwargs):
        got.update(kwargs)
        return True, "done"

    postprocessing.execute_postprocessing_plugin(
        plugin_name="kw", plugin_func=plugin, params={},
        results_dir="/r", config_dir="/c", should_stop=lambda: False)

    assert "should_stop" in got
