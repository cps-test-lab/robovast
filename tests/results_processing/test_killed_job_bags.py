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

from robovast.results_processing.data.rosbags_common import (
    is_under_tolerated_root, resolve_tolerated_roots)
from robovast.results_processing.postprocessing_plugins import _killed_job_dirs


def _ledger(campaign, entries):
    exec_dir = campaign / "_execution"
    exec_dir.mkdir(parents=True, exist_ok=True)
    (exec_dir / "killed_jobs.json").write_text(json.dumps(entries))


def test_no_ledger_means_nothing_to_tolerate(tmp_path):
    """The default path: a campaign nobody intervened in passes no flags at all."""
    assert _killed_job_dirs(str(tmp_path)) == []


def test_killed_job_dirs_are_read_from_the_ledger(tmp_path):
    _ledger(tmp_path, [
        {"job_dir": "_jobs/batch-0/job-2", "job_name": "j2", "source": "webui",
         "reason": "wedged", "runs": []},
        {"job_dir": "_jobs/batch-0/job-5", "job_name": "j5", "source": "mcp",
         "reason": None, "runs": []},
    ])
    assert _killed_job_dirs(str(tmp_path)) == ["_jobs/batch-0/job-2",
                                               "_jobs/batch-0/job-5"]


def test_the_same_job_killed_twice_is_listed_once(tmp_path):
    """Deduplicated: the flag is a path filter, and repeating it says nothing extra."""
    _ledger(tmp_path, [
        {"job_dir": "_jobs/batch-0/job-2", "job_name": "j2", "source": "cli",
         "reason": "a", "runs": []},
        {"job_dir": "_jobs/batch-0/job-2", "job_name": "j2", "source": "cli",
         "reason": "b", "runs": []},
    ])
    assert _killed_job_dirs(str(tmp_path)) == ["_jobs/batch-0/job-2"]


def test_a_local_kill_with_no_job_dir_contributes_nothing(tmp_path):
    """The local lane records a run key and may have no job dir; that is not a path filter."""
    _ledger(tmp_path, [{"job_dir": "", "job_name": "cfgA/0", "source": "webui",
                        "reason": "x", "runs": ["cfgA/0"]}])
    assert _killed_job_dirs(str(tmp_path)) == []


def test_a_broken_ledger_never_breaks_postprocessing(tmp_path):
    """Postprocessing is the campaign's deliverable; a bad annotation must not cost it."""
    exec_dir = tmp_path / "_execution"
    exec_dir.mkdir(parents=True)
    (exec_dir / "killed_jobs.json").write_text("{ not json")
    assert _killed_job_dirs(str(tmp_path)) == []


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
    pairs = [(cmd[i], cmd[i + 1]) for i, a in enumerate(cmd) if a == "--tolerate-under"]
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
