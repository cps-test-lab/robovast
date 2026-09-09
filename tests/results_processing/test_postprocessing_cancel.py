# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Stopping a campaign while it postprocesses, and what that must leave behind.

A stop used to be *checked* only before postprocessing started, so a campaign stopped once
it had begun ran its conversion to the end and reported the stop as done.

Cancelling it is only safe because of WHERE the cancellation lands, which is what these
tests pin. The containerised conversion is the one step killed in flight, and it is written
to survive that: a bag records itself as converted only after its handlers finish, and every
output is rewritten rather than appended. Everything after it -- the index ingest, the
provenance record, the metadata -- is cancelled only *between* steps. So a cancelled
campaign is never half-indexed and never claims derived data it does not have.
"""

import pytest

from robovast.common.campaign_data import campaign_has_derived_data
from robovast.results_processing import postprocessing
from robovast.results_processing.postprocessing import POSTPROCESSING_CANCELLED


def _campaign_tree(tmp_path):
    """The smallest results tree ``run_postprocessing`` will walk."""
    root = tmp_path / "camp-2026-01-01-000000"
    run_dir = root / "goal-1" / "0"
    run_dir.mkdir(parents=True)
    (run_dir / "nav_metrics.csv").write_text("duration_s,collided\n12.5,0\n")
    (root / "_config").mkdir()
    (root / "_config" / "campaign.vast").write_text(
        "execution:\n  containers: {}\nresults_processing:\n  postprocessing: []\n")
    return root


def test_a_cancelled_run_never_reaches_the_index(tmp_path, monkeypatch):
    """The ingest is the step that must not be entered part-way.

    No database is configured in this test and none is needed: reaching the ingest at all
    would raise ``IndexUnreachableError`` rather than return, so the plain cancelled return
    is itself the assertion that the campaign was never indexed.
    """
    from robovast.common import index_db

    monkeypatch.delenv(index_db.DSN_ENV, raising=False)
    root = _campaign_tree(tmp_path)

    ok, message = postprocessing.run_postprocessing(
        str(tmp_path), campaign=root.name, should_stop=lambda: True)

    assert ok is False
    assert message == POSTPROCESSING_CANCELLED


def test_a_cancelled_run_does_not_claim_the_campaign_is_postprocessed(tmp_path, monkeypatch):
    """The provenance record is the evidence a campaign carries derived data.

    Written last, after the ingest, precisely so its presence means every step succeeded --
    so a cancelled run must stop before it. Otherwise the campaign would read as
    postprocessed, its archive would be named as such, and nobody would re-run the step
    that never finished.
    """
    from robovast.common import index_db

    monkeypatch.delenv(index_db.DSN_ENV, raising=False)
    root = _campaign_tree(tmp_path)

    postprocessing.run_postprocessing(str(tmp_path), campaign=root.name,
                                      should_stop=lambda: True)

    assert campaign_has_derived_data(str(root)) is False


def test_a_stop_arriving_mid_run_is_taken_at_the_next_step_boundary(tmp_path, monkeypatch):
    """A stop during step N ends the run at N+1 rather than at the end of the pipeline.

    The steps themselves still run to completion here — the predicate is polled between
    them — which is the property that keeps a cancellation clean for every step that is not
    the interruptible one.
    """
    from robovast.common import index_db

    monkeypatch.delenv(index_db.DSN_ENV, raising=False)
    root = _campaign_tree(tmp_path)
    calls = {"n": 0}

    def should_stop():
        calls["n"] += 1
        return calls["n"] > 1          # runs the first step, then stops

    ok, message = postprocessing.run_postprocessing(
        str(tmp_path), campaign=root.name, should_stop=should_stop)

    assert (ok, message) == (False, POSTPROCESSING_CANCELLED)
    assert calls["n"] > 1              # it did not give up before doing anything
    assert campaign_has_derived_data(str(root)) is False


def test_no_predicate_leaves_the_pipeline_exactly_as_it_was(tmp_path, monkeypatch):
    """Without a predicate nothing is polled and nothing is skipped.

    The re-run entry points postprocess campaigns nothing is driving, so ``None`` has to
    mean "run it all", not "cancel immediately".
    """
    from robovast.common import index_db

    monkeypatch.delenv(index_db.DSN_ENV, raising=False)
    root = _campaign_tree(tmp_path)

    from robovast.common.errors import IndexUnreachableError
    with pytest.raises(IndexUnreachableError):
        # Reaching the ingest at all is the point: the run was not cancelled.
        postprocessing.run_postprocessing(str(tmp_path), campaign=root.name,
                                          skip_metadata=True)


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


# -- the one step that is killed in flight -----------------------------------
#
# Cancelling between steps is not enough on its own: the rosbag conversion is a single step
# and the long one, so a stop that waited for it to end would be no stop at all on the
# campaigns big enough to want stopping.

def _gone(pid, timeout=5.0):
    """Whether *pid* is gone within *timeout*, allowing for the reaping that follows a kill."""
    import os
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            return True
        time.sleep(0.05)
    return False


def test_a_cancelled_step_takes_the_whole_process_group_with_it():
    """The group, not the process.

    ``docker_exec.sh`` waits on a foreground ``docker run``, and bash defers a trap until
    its foreground child returns -- so signalling the script alone would sit unhandled for
    exactly as long as the conversion being cancelled, and the container would keep going.
    The shape is reproduced here with a shell whose own child outlives it: killing the group
    is what reaches that child.
    """
    import subprocess

    from robovast.results_processing import postprocessing_plugins as plugins

    process = subprocess.Popen(  # noqa: S603
        ["bash", "-c", "sleep 30 & echo $!; wait"],  # noqa: S607
        stdout=subprocess.PIPE, text=True, start_new_session=True)
    grandchild = int(process.stdout.readline())

    with plugins._cancelled_by(lambda: True, process):
        process.wait(timeout=20)

    assert process.returncode != 0          # signalled, not a clean exit
    assert _gone(grandchild)                # ...and so was the child it was waiting on


def test_a_step_nobody_cancels_runs_to_its_own_end():
    """The watch must not be able to end a conversion nobody stopped."""
    import subprocess

    from robovast.results_processing import postprocessing_plugins as plugins

    process = subprocess.Popen(["bash", "-c", "exit 7"],  # noqa: S603,S607
                               start_new_session=True)
    with plugins._cancelled_by(lambda: False, process):
        process.wait(timeout=20)

    assert process.returncode == 7


def test_no_predicate_starts_no_watching_thread():
    """The ordinary path -- a CLI run, a campaign nobody stops -- pays nothing for this."""
    import subprocess
    import threading

    from robovast.results_processing import postprocessing_plugins as plugins

    process = subprocess.Popen(["bash", "-c", "exit 0"],  # noqa: S603,S607
                               start_new_session=True)
    before = threading.active_count()
    with plugins._cancelled_by(None, process):
        assert threading.active_count() == before
    process.wait(timeout=20)
