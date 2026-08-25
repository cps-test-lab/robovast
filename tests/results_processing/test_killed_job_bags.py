# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A job stopped by hand leaves an unreadable rosbag — and that must not fail the step.

Killing a job SIGKILLs its pod mid-write, so the rosbag it was recording is never
finalized and can never be opened. Before this, that one bag failed the campaign's whole
postprocessing step: using the per-job stop cost the metrics of every job that *did*
finish, which is the opposite of what the feature is for.

So ``rosbags_process`` is told which dirs belong to killed jobs (``--tolerate-under``) and
counts their errors apart from real ones. It still *attempts* them — a kill that landed
between bags leaves readable ones behind, and their data is worth having.
"""

import json
import os

from robovast.results_processing.data.rosbags_common import (is_under_tolerated_root,
                                                             resolve_tolerated_roots)
from robovast.results_processing.postprocessing_plugins import _interrupted_job_dirs


def _ledger(campaign, entries):
    """One ledger for every kind of intervention, so each entry declares which it is."""
    exec_dir = campaign / "_execution"
    exec_dir.mkdir(parents=True, exist_ok=True)
    tagged = [{"kind": "killed", **e} for e in entries]
    (exec_dir / "interventions.json").write_text(json.dumps(tagged))


def test_no_ledger_means_nothing_to_tolerate(tmp_path):
    """The default path: a campaign nobody intervened in passes no flags at all."""
    assert _interrupted_job_dirs(str(tmp_path)) == []


def test_killed_job_dirs_are_read_from_the_ledger(tmp_path):
    _ledger(tmp_path, [
        {"job_dir": "_jobs/batch-0/job-2", "job_name": "j2", "source": "webui",
         "reason": "wedged", "runs": []},
        {"job_dir": "_jobs/batch-0/job-5", "job_name": "j5", "source": "mcp",
         "reason": None, "runs": []},
    ])
    assert _interrupted_job_dirs(str(tmp_path)) == ["_jobs/batch-0/job-2",
                                               "_jobs/batch-0/job-5"]


def test_the_same_job_killed_twice_is_listed_once(tmp_path):
    """Deduplicated: the flag is a path filter, and repeating it says nothing extra."""
    _ledger(tmp_path, [
        {"job_dir": "_jobs/batch-0/job-2", "job_name": "j2", "source": "cli",
         "reason": "a", "runs": []},
        {"job_dir": "_jobs/batch-0/job-2", "job_name": "j2", "source": "cli",
         "reason": "b", "runs": []},
    ])
    assert _interrupted_job_dirs(str(tmp_path)) == ["_jobs/batch-0/job-2"]


def test_a_local_kill_with_no_job_dir_contributes_nothing(tmp_path):
    """The local lane records a run key and may have no job dir; that is not a path filter."""
    _ledger(tmp_path, [{"job_dir": "", "job_name": "cfgA/0", "source": "webui",
                        "reason": "x", "runs": ["cfgA/0"]}])
    assert _interrupted_job_dirs(str(tmp_path)) == []


def test_a_broken_ledger_never_breaks_postprocessing(tmp_path):
    """Postprocessing is the campaign's deliverable; a bad annotation must not cost it."""
    exec_dir = tmp_path / "_execution"
    exec_dir.mkdir(parents=True)
    (exec_dir / "killed_jobs.json").write_text("{ not json")
    assert _interrupted_job_dirs(str(tmp_path)) == []


# -- which bags are tolerated ------------------------------------------------------------
#
# The predicate lives in ``rosbags_common`` rather than in ``rosbags_process.py``, which
# cannot be imported here at all: it pulls in ``rosbag2_py`` at module level and only ever
# runs inside the ROS execution container.


def test_a_killed_jobs_bag_is_tolerated(tmp_path):
    roots = resolve_tolerated_roots(str(tmp_path), ["_jobs/batch-0/job-2"])
    bag = str(tmp_path / "_jobs" / "batch-0" / "job-2" / "logs" / "rosout_bag")
    assert is_under_tolerated_root(bag, roots) is True


def test_a_surviving_jobs_bag_is_not_tolerated(tmp_path):
    """The whole point: a real handler error still fails the step."""
    roots = resolve_tolerated_roots(str(tmp_path), ["_jobs/batch-0/job-2"])
    assert is_under_tolerated_root(str(tmp_path / "goal-1" / "0" / "rosbag2"), roots) is False


def test_a_sibling_job_with_a_longer_number_is_not_swallowed(tmp_path):
    """``job-2`` must not match ``job-20`` — the separator is part of the test.

    A packed campaign really does have both, so a plain ``startswith`` would silently
    stop reporting genuine failures in nine of its jobs.
    """
    roots = resolve_tolerated_roots(str(tmp_path), ["_jobs/batch-0/job-2"])
    bag = str(tmp_path / "_jobs" / "batch-0" / "job-20" / "logs" / "rosout_bag")
    assert is_under_tolerated_root(bag, roots) is False


def test_nothing_is_tolerated_without_the_flag(tmp_path):
    roots = resolve_tolerated_roots(str(tmp_path), [])
    assert roots == []
    assert is_under_tolerated_root(str(tmp_path / "any" / "bag"), roots) is False


def test_the_plugin_passes_a_tolerate_flag_per_killed_job(tmp_path, monkeypatch):
    """End of the wire: the ledger on disk becomes flags on the container command."""
    from robovast.results_processing import postprocessing_plugins as pp

    _ledger(tmp_path, [
        {"job_dir": "_jobs/batch-0/job-2", "job_name": "j2", "source": "webui",
         "reason": "wedged", "runs": []},
        {"job_dir": "_jobs/batch-0/job-5", "job_name": "j5", "source": "mcp",
         "reason": None, "runs": []},
    ])
    seen = {}

    class _Proc:
        stdout = iter(["Summary: 0 rosbags (0 success, 0 errors, 0 no-data)"])

        def wait(self):
            return 0

    def _popen(cmd, **_kwargs):
        seen["cmd"] = cmd
        return _Proc()

    monkeypatch.setattr(pp.subprocess, "Popen", _popen)
    ok, _message = pp.RosbagsProcess()(
        results_dir=str(tmp_path), config_dir=str(tmp_path),
        plugins=[{"type": "rosout_to_csv"}])

    assert ok
    cmd = seen["cmd"]
    pairs = [(a, cmd[i + 1]) for i, a in enumerate(cmd) if a == "--tolerate-under"]
    assert pairs == [("--tolerate-under", "_jobs/batch-0/job-2"),
                     ("--tolerate-under", "_jobs/batch-0/job-5")]


def test_the_plugin_passes_no_flag_for_an_untouched_campaign(tmp_path, monkeypatch):
    """The default path stays exactly the command it was before this existed."""
    from robovast.results_processing import postprocessing_plugins as pp

    seen = {}

    class _Proc:
        stdout = iter(["Summary: 0 rosbags (0 success, 0 errors, 0 no-data)"])

        def wait(self):
            return 0

    monkeypatch.setattr(pp.subprocess, "Popen",
                        lambda cmd, **_k: (seen.update(cmd=cmd), _Proc())[1])
    pp.RosbagsProcess()(results_dir=str(tmp_path), config_dir=str(tmp_path),
                        plugins=[{"type": "rosout_to_csv"}])

    assert "--tolerate-under" not in seen["cmd"]


def test_an_invalidated_jobs_bag_is_tolerated_too(tmp_path):
    """A job the runner invalidated is deleted at grace_period_seconds=0, exactly as a
    stopped one is -- so its recorder was SIGKILLed mid-write and its bag is unopenable for
    the identical reason. Treating only the operator's kill would mean one crashed sidecar
    still costs the whole campaign's metrics, which is the bug one layer down."""
    _ledger(tmp_path, [
        {"kind": "killed", "job_dir": "_jobs/batch-0/job-1", "job_name": "j1",
         "source": "cli", "reason": None, "runs": []},
        {"kind": "invalid", "job_dir": "_jobs/batch-0/job-3", "job_name": "j3",
         "source": "runner", "detail": "ContainerRestarted: ... (exit 135, SIGBUS)",
         "runs": ["cfgA/0"]},
    ])
    assert _interrupted_job_dirs(str(tmp_path)) == ["_jobs/batch-0/job-1",
                                                    "_jobs/batch-0/job-3"]


def test_a_probe_does_not_make_a_bag_unreadable(tmp_path):
    """`probed` is the third kind and the run carried on afterwards: its bag is fine, and
    tolerating it would hide a genuine conversion error."""
    _ledger(tmp_path, [{"kind": "probed", "job_dir": "_jobs/batch-0/job-4",
                        "job_name": "j4", "source": "mcp", "runs": []}])
    assert _interrupted_job_dirs(str(tmp_path)) == []


# -- what a failing bag tells the reader ---------------------------------------------------
#
# Workers run under ``redirect_stdout`` so 32 of them cannot shred the progress bar. That
# buffer used to be a throwaway: every ``✗`` a handler printed died in the worker while its
# *count* came home in the ``-2`` sentinel, so the summary reported "N handler error(s) — see
# the messages above" with nothing above it. On the cluster lane, which never passes
# ``--debug``, that happened on every campaign. Same shape as the reporting bug it sat next
# to: a pointer to evidence nobody kept.


def test_a_failing_bag_reports_what_it_printed(tmp_path):
    from robovast.results_processing.data.rosbags_common import failing_bag_output
    bag = str(tmp_path / "goal-1" / "0" / "rosbag2")
    out = failing_bag_output(
        [(bag, "  ✗ Handler Nav2BtTree on_end error: no such column\n")], str(tmp_path), [])
    assert out == [(os.path.join("goal-1", "0", "rosbag2"),
                    "  ✗ Handler Nav2BtTree on_end error: no such column")]


def test_a_bag_that_said_nothing_is_not_reported(tmp_path):
    """Absence of output is not an error to print — the count still stands on its own."""
    from robovast.results_processing.data.rosbags_common import failing_bag_output
    bag = str(tmp_path / "goal-1" / "0" / "rosbag2")
    assert failing_bag_output([(bag, "   \n")], str(tmp_path), []) == []


def test_a_tolerated_bag_does_not_bury_the_real_errors(tmp_path):
    """A campaign with twenty stopped jobs prints the same "failed to open" twenty times.
    They are expected, counted apart, and explained by the summary's NOTE."""
    from robovast.results_processing.data.rosbags_common import failing_bag_output
    roots = resolve_tolerated_roots(str(tmp_path), ["_jobs/batch-0/job-2"])
    killed = str(tmp_path / "_jobs" / "batch-0" / "job-2" / "logs" / "rosout_bag")
    real = str(tmp_path / "goal-1" / "0" / "rosbag2")
    out = failing_bag_output([(killed, "✗ failed to open — unfinalized\n"),
                              (real, "  ✗ Handler X on_end error: boom\n")],
                             str(tmp_path), roots)
    assert [rel for rel, _ in out] == [os.path.join("goal-1", "0", "rosbag2")]


def test_the_error_pointer_never_points_at_nothing():
    """"We looked and the workers said nothing" and "we never looked" are different facts."""
    from robovast.results_processing.data.rosbags_common import handler_error_pointer
    assert handler_error_pointer(True) == "see the error output above"
    assert "printed nothing" in handler_error_pointer(False)


# -- a bag's outcome, and the sentinel that decides the exit code --------------------------


def test_a_bag_no_handler_would_start_is_a_failure_not_an_empty_bag():
    """It returned 0 — "opened fine, produced nothing" — so the aggregate tallied it as
    no-data, never counted it in ``error_bags``, and the step exited 0. A bag whose every
    handler threw in on_begin was reported as a successful conversion, which is precisely
    what the exit-code contract in ``rosbags_process`` says must not happen.
    """
    from robovast.results_processing.data.rosbags_common import FAILED, BagResult
    result = BagResult("/b/rosbag2", FAILED, output="  ✗ Handler X on_begin failed: boom\n")
    assert result.failed is True
    assert result.cached is False


def test_a_bag_that_opened_and_wrote_nothing_is_not_a_failure():
    """The other half of the distinction: 0 records is a real, non-failing outcome, and
    conflating it with FAILED would fail campaigns that simply recorded an empty topic."""
    from robovast.results_processing.data.rosbags_common import BagResult
    empty = BagResult("/b/rosbag2", 0)
    assert empty.failed is False and empty.cached is False


def test_a_cache_hit_is_neither():
    from robovast.results_processing.data.rosbags_common import CACHED, BagResult
    hit = BagResult("/b/rosbag2", CACHED)
    assert hit.cached is True and hit.failed is False
    assert hit.handler_results == () and hit.output == ""


def test_a_result_that_omits_a_field_degrades_rather_than_raising():
    """Why this is a NamedTuple with defaults: the worker returns from five places and the
    parent reads it in four. Widening a bare tuple once already left a three-tuple meeting
    a four-way unpack — a ValueError reachable only inside the ROS container, where no test
    goes."""
    from robovast.results_processing.data.rosbags_common import FAILED, BagResult
    assert BagResult("/b", FAILED).output == ""


def test_the_empty_default_is_not_shared_mutable_state():
    from robovast.results_processing.data.rosbags_common import BagResult
    a, b = BagResult("/x", 0), BagResult("/y", 0)
    assert a.handler_results is b.handler_results
    assert isinstance(a.handler_results, tuple)  # a shared mutable default is its own bug


def test_a_result_survives_the_pool_boundary():
    """It is returned from a worker process, so it has to pickle — which rules out a type
    defined anywhere but module scope."""
    import pickle
    from robovast.results_processing.data.rosbags_common import FAILED, BagResult
    result = BagResult("/b", FAILED, [(3, ["a.csv"])], "boom\n")
    assert pickle.loads(pickle.dumps(result)) == result
