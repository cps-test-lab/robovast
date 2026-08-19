# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Phase 1.4d/4e: get_campaign_status surfaces search state and reports honest
overall progress (never a batch-scoped ratio presented as completion)."""

from robovast.execution.control_server import Status
from robovast.mcp_server.plugins import execution as cc


def _status(**kw):
    return Status(**kw)


# -- 4e: overall progress is mode-aware and honest ---------------------------


def test_batch_progress_is_completed_over_total():
    st = _status(phase="running", mode="batch",
                 runs={"completed": 3, "total": 10})
    d = cc._status_to_dict("camp", "local", st)
    assert d["batch_runs_done"] == 3 and d["batch_runs_total"] == 10
    assert d["progress"] == 0.3


def test_search_progress_comes_from_budget_not_run_ratio():
    # A search batch is 3/10 runs in, but overall progress is governed by the
    # stopping criteria, NOT that ratio.
    st = _status(phase="running", mode="search",
                 runs={"completed": 3, "total": 10},
                 budget=[{"label": "batches", "current": 2, "limit": 10, "done": False},
                         {"label": "time_s", "current": 30, "limit": 300, "done": False}])
    d = cc._status_to_dict("camp", "local", st)
    assert d["progress"] == 0.2  # max(2/10, 30/300) — not 3/10
    assert d["batch_runs_done"] == 3  # still exposed, but clearly batch-scoped


def test_search_progress_is_null_when_unknowable():
    # Search with no usable budget value: must NOT invent a number.
    st = _status(phase="running", mode="search",
                 runs={"completed": 3, "total": 10},
                 budget=[{"label": "metric", "current": None, "limit": 1.0, "done": False}])
    d = cc._status_to_dict("camp", "local", st)
    assert d["progress"] is None


def test_search_without_budget_is_null_not_run_ratio():
    st = _status(phase="running", mode="search",
                 runs={"completed": 3, "total": 10})
    d = cc._status_to_dict("camp", "local", st)
    assert d["progress"] is None


# -- 4d: rich search fields are surfaced --------------------------------------


def test_rich_search_fields_passed_through():
    st = _status(phase="finished", mode="search",
                 batches_done=7, best_objective=0.42,
                 runs={"completed": 5, "total": 5},
                 budget=[{"label": "batches", "current": 7, "limit": 7, "done": True}],
                 stop={"kind": "batches", "reason": "max batches reached"})
    d = cc._status_to_dict("camp", "service", st)
    assert d["mode"] == "search"
    assert d["batches_done"] == 7
    assert d["best_objective"] == 0.42
    assert d["stop"] == {"kind": "batches", "reason": "max batches reached"}
    assert d["budget"][0]["label"] == "batches"


def test_batch_mode_omits_search_only_fields():
    st = _status(phase="finished", mode="batch",
                 runs={"completed": 4, "total": 4})
    d = cc._status_to_dict("camp", "local", st)
    assert d["progress"] == 1.0
    assert "best_objective" not in d
    assert "stop" not in d


# -- durable source: a finished local search surfaces rich state after restart --


def test_finished_local_search_reads_rich_state_from_outcome(tmp_path):
    """A finished campaign's outcome.json is the durable full Status; the local
    branch must surface its search state (not just a bare 'finished')."""
    from robovast.common.campaign_data import read_execution_outcome, write_execution_outcome
    st = _status(phase="finished", mode="search", batches_done=5,
                 best_objective=0.13, runs={"completed": 4, "total": 4},
                 budget=[{"label": "batches", "current": 5, "limit": 5, "done": True}],
                 stop={"kind": "batches", "reason": "budget exhausted"})
    write_execution_outcome(tmp_path, st)

    reloaded = read_execution_outcome(tmp_path)  # what get_campaign_status reads
    d = cc._status_to_dict("camp", "local", reloaded)
    assert d["mode"] == "search"
    assert d["best_objective"] == 0.13
    assert d["stop"]["kind"] == "batches"
    assert d["progress"] == 1.0  # 5/5 budget
