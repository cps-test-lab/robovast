# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A campaign's infrastructure log is read as rows after a cursor, from its phase files.

The files stay the record -- one per phase under ``_execution/``, a repeated phase's earlier
runs under ``sections/`` -- and a reader takes them in the order the work happened, one row per
stamped line with its continuation lines under it. The cursor is where each file has been read
to, so a reader that comes back sees what arrived since, and a filter narrows what it is shown
without moving where it continues from.
"""

import os
import time
from pathlib import Path

import pytest

from robovast.common.campaign_logs import EXECUTION_DIR, section_name
from robovast.service import campaign_log
from robovast.service.campaign_log import read_rows
from tests.service.null_service import NullService
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore

CID = "campaign-2026-08-01-090000"


def _stamp(t, level, message, logger="robovast.execution.controller"):
    """A line as the campaign log handler writes it, at second *t* of the hour."""
    return f"2026-08-01 09:{t // 60:02d}:{t % 60:02d} {level} {logger}: {message}\n"


def _campaign(tmp_path, files: dict, *, aged=True) -> Path:
    """A campaign directory whose ``_execution/`` holds *files* (name -> text)."""
    root = tmp_path / CID
    for name, text in files.items():
        path = root / EXECUTION_DIR / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        if aged:
            _age(path)
    return root


def _age(*paths):
    past = time.time() - 60
    for path in paths:
        os.utime(path, (past, past))


def _messages(read):
    return [row.message for row in read.rows]


# -- the rows -------------------------------------------------------------------------------

def test_the_phases_are_read_in_the_order_they_ran(tmp_path):
    root = _campaign(tmp_path, {
        "postprocessing.log": _stamp(30, "INFO", "tables built"),
        "controller.log": _stamp(20, "INFO", "batch 0"),
        "variation.log": _stamp(10, "INFO", "3 configs"),
        "build.log": "waiting for image sim:v3 (build b-1)\n#1 [internal] load\n",
    })
    read = read_rows(root, final=True)
    assert [(r.phase, r.message) for r in read.rows] == [
        ("BUILD", "waiting for image sim:v3 (build b-1)"), ("BUILD", "#1 [internal] load"),
        ("VARIATION", "3 configs"), ("RUN", "batch 0"), ("POSTPROCESSING", "tables built")]
    assert [r.seq for r in read.rows] == [0, 1, 2, 3, 4]
    assert read.phases == ["BUILD", "VARIATION", "RUN", "POSTPROCESSING"]
    assert read.pending is False


def test_a_stamped_line_is_a_row_with_its_stamp_level_and_logger(tmp_path):
    root = _campaign(tmp_path, {"controller.log": _stamp(65, "WARNING", "slow node",
                                                         logger="robovast.execution.x")})
    row = read_rows(root, final=True).rows[0]
    assert (row.level, row.logger, row.message) == ("WARNING", "robovast.execution.x",
                                                    "slow node")
    assert row.wall_ts == time.mktime(time.strptime("2026-08-01 09:01:05",
                                                    "%Y-%m-%d %H:%M:%S"))


def test_an_unstamped_line_continues_the_stamped_record_above_it(tmp_path):
    root = _campaign(tmp_path, {"controller.log": (
        _stamp(1, "ERROR", "run 0 failed") + "Traceback (most recent call last):\n"
        + "  File x.py\n" + _stamp(2, "INFO", "next"))})
    read = read_rows(root, final=True)
    assert _messages(read) == ["run 0 failed\nTraceback (most recent call last):\n  File x.py",
                               "next"]
    assert [r.seq for r in read.rows] == [0, 1]


def test_output_with_no_record_above_it_is_a_note_row_per_line(tmp_path):
    """Build and pip output: no stamp, so nothing to say where a row ends, so a line each."""
    root = _campaign(tmp_path, {"build.log": "#1 load\n#2 CACHED\n"})
    rows = read_rows(root, final=True).rows
    assert [(r.level, r.logger, r.wall_ts, r.message) for r in rows] == [
        ("NOTE", "", None, "#1 load"), ("NOTE", "", None, "#2 CACHED")]


def test_compose_output_relayed_into_the_controller_log_keeps_its_own_stamp(tmp_path):
    """Locally the run's containers are teed into ``controller.log``; their lines carry the
    ROS stamp, which is a row of its own, not a continuation of the controller's."""
    root = _campaign(tmp_path, {"controller.log": (
        _stamp(1, "INFO", "starting compose")
        + "robovast  | [WARN] [1785092240.500000] [tf_bridge]: TF_OLD_DATA\n"
        + "robovast  | plain compose text\n")})
    rows = read_rows(root, final=True).rows
    assert [(r.level, r.logger, r.message) for r in rows] == [
        ("INFO", "robovast.execution.controller", "starting compose"),
        ("WARN", "tf_bridge", "TF_OLD_DATA\nrobovast  | plain compose text")]
    assert rows[1].wall_ts == pytest.approx(1785092240.5)


def test_a_repeated_phase_reads_after_the_one_that_followed_it_the_first_time(tmp_path):
    root = _campaign(tmp_path, {
        "controller.log": _stamp(1, "INFO", "ran"),
        section_name(1, "postprocessing.log"): _stamp(2, "INFO", "first postprocess"),
        section_name(2, "share.log"): _stamp(3, "INFO", "shared"),
        "postprocessing.log": _stamp(4, "INFO", "second postprocess"),
    })
    read = read_rows(root, final=True)
    assert [(r.phase, r.message) for r in read.rows] == [
        ("RUN", "ran"), ("POSTPROCESSING", "first postprocess"), ("SHARE", "shared"),
        ("POSTPROCESSING", "second postprocess")]
    assert read.phases == ["RUN", "POSTPROCESSING", "SHARE"]


def test_a_campaign_with_no_log_yet_reads_as_empty(tmp_path):
    read = read_rows(tmp_path / "never-here", final=False)
    assert (read.rows, read.cursor, read.pending, read.phases) == ([], "", False, [])


# -- the filters ----------------------------------------------------------------------------

@pytest.fixture(name="mixed")
def _mixed(tmp_path):
    return _campaign(tmp_path, {
        "build.log": "#1 load\nERROR: failed to solve\n",
        "controller.log": (_stamp(1, "INFO", "batch 0") + _stamp(2, "WARNING", "slow")
                           + _stamp(3, "ERROR", "run 1 failed", logger="robovast.runs")),
    })


def test_phase_keeps_one_phase_and_still_names_the_others(mixed):
    read = read_rows(mixed, final=True, phase="run")
    assert {r.phase for r in read.rows} == {"RUN"}
    assert read.phases == ["BUILD", "RUN"]
    assert _messages(read_rows(mixed, final=True, phase="all")) == _messages(
        read_rows(mixed, final=True))


def test_min_level_ranks_a_stamped_row_by_its_level_and_a_note_by_the_classifier(mixed):
    assert _messages(read_rows(mixed, final=True, min_level="WARNING")) == [
        "ERROR: failed to solve", "slow", "run 1 failed"]
    # The classifier never claims an error a line did not mark, so the build's line stops at
    # a WARNING floor, the same as it does on every other log surface.
    assert _messages(read_rows(mixed, final=True, min_level="error")) == ["run 1 failed"]


def test_grep_matches_the_message_or_the_logger_case_insensitively(mixed):
    assert _messages(read_rows(mixed, final=True, grep="FAILED")) == [
        "ERROR: failed to solve", "run 1 failed"]
    assert _messages(read_rows(mixed, final=True, grep="^robovast\\.runs$")) == ["run 1 failed"]


def test_a_filtered_read_keeps_every_rows_place_in_the_whole_log(mixed):
    """``seq`` is the row's position in the log, not in what the filter kept."""
    assert [r.seq for r in read_rows(mixed, final=True, min_level="warn").rows] == [1, 3, 4]


@pytest.mark.parametrize("kwargs, match", [
    ({"phase": "biuld"}, "unknown phase"),
    ({"min_level": "loud"}, "unknown level"),
    ({"grep": "["}, "not a valid regular expression"),
])
def test_a_filter_the_reader_does_not_know_is_refused(mixed, kwargs, match):
    with pytest.raises(ValueError, match=match):
        read_rows(mixed, final=True, **kwargs)


# -- the cursor -----------------------------------------------------------------------------

def test_a_read_from_the_cursor_returns_what_arrived_since(tmp_path):
    root = _campaign(tmp_path, {"controller.log": _stamp(1, "INFO", "one")})
    first = read_rows(root, final=True)
    assert _messages(first) == ["one"]
    again = read_rows(root, first.cursor, final=True)
    assert (again.rows, again.cursor) == ([], first.cursor)

    log = root / EXECUTION_DIR / "controller.log"
    with open(log, "a") as handle:
        handle.write(_stamp(2, "INFO", "two"))
    _age(log)
    (root / EXECUTION_DIR / "postprocessing.log").write_text(_stamp(3, "INFO", "three"))
    _age(root / EXECUTION_DIR / "postprocessing.log")
    later = read_rows(root, first.cursor, final=True)
    assert [(r.seq, r.message) for r in later.rows] == [(1, "two"), (2, "three")]


def test_a_filtered_read_advances_the_cursor_over_what_it_skipped(mixed):
    narrow = read_rows(mixed, final=True, phase="build")
    assert _messages(read_rows(mixed, narrow.cursor, final=True)) == []


def test_the_cursor_follows_a_live_phase_file_into_its_archived_section(tmp_path):
    """A rerun archives the finished file under ``sections/`` before it starts. The entry
    follows the bytes, so they are not read twice, and the new run's file starts at zero."""
    root = _campaign(tmp_path, {"controller.log": _stamp(1, "INFO", "ran"),
                                "postprocessing.log": _stamp(2, "INFO", "first")})
    before = read_rows(root, final=True)
    exec_dir = root / EXECUTION_DIR
    (exec_dir / "sections").mkdir()
    (exec_dir / "postprocessing.log").replace(exec_dir / section_name(1, "postprocessing.log"))
    (exec_dir / "postprocessing.log").write_text(_stamp(3, "INFO", "second"))
    _age(exec_dir / "postprocessing.log")

    after = read_rows(root, before.cursor, final=True)
    assert [(r.seq, r.phase, r.message) for r in after.rows] == [(2, "POSTPROCESSING", "second")]
    assert _messages(read_rows(root, after.cursor, final=True)) == []


def test_a_cursor_this_service_did_not_issue_is_refused(tmp_path):
    with pytest.raises(ValueError, match="cursor"):
        read_rows(tmp_path, "not-a-cursor!", final=True)


# -- a file still being written -------------------------------------------------------------

def test_the_last_record_of_a_hot_file_is_held_back_until_it_settles(tmp_path):
    root = _campaign(tmp_path, {"controller.log": _stamp(1, "INFO", "done")
                                + _stamp(2, "ERROR", "failed")}, aged=False)
    now = time.time()
    hot = read_rows(root, final=False, now=now)
    assert (_messages(hot), hot.pending) == (["done"], True)
    # A traceback arrives under it; the reader's hold-back is what keeps it one row.
    with open(root / EXECUTION_DIR / "controller.log", "a") as handle:
        handle.write("Traceback\n")
    settled = read_rows(root, hot.cursor, final=False,
                        now=time.time() + campaign_log.SETTLE_S)
    assert [(r.seq, r.message) for r in settled.rows] == [(1, "failed\nTraceback")]
    assert settled.pending is False


def test_a_finished_campaign_sends_everything_partial_line_included(tmp_path):
    root = _campaign(tmp_path, {"controller.log": _stamp(1, "INFO", "one") + "no newline"},
                     aged=False)
    read = read_rows(root, final=True)
    assert (_messages(read), read.pending) == (["one\nno newline"], False)


def test_a_line_without_its_newline_waits_for_it_while_the_campaign_runs(tmp_path):
    root = _campaign(tmp_path, {"controller.log": _stamp(1, "INFO", "one") + "part"})
    read = read_rows(root, final=False)
    assert (_messages(read), read.pending) == (["one"], True)


def test_a_later_file_starts_past_the_row_a_hot_file_holds_back(tmp_path):
    """The held-back record keeps its number, so the sequence stays dense across files."""
    root = _campaign(tmp_path, {"controller.log": _stamp(1, "INFO", "a") + _stamp(2, "INFO", "b"),
                                "postprocessing.log": _stamp(3, "INFO", "c")}, aged=False)
    _age(root / EXECUTION_DIR / "postprocessing.log")
    hot = read_rows(root, final=False, now=time.time())
    assert [(r.seq, r.message) for r in hot.rows] == [(0, "a"), (2, "c")]
    later = read_rows(root, hot.cursor, final=False, now=time.time() + campaign_log.SETTLE_S)
    assert [(r.seq, r.message) for r in later.rows] == [(1, "b")]


# -- through the service --------------------------------------------------------------------

@pytest.fixture(name="transport")
def _transport(tmp_path):
    store = WorkspaceStore(registry=WorkspaceRegistry(root=tmp_path / "workspaces"))
    return NullService(store=store, results_dir=str(tmp_path / "results"))


def _live(transport, monkeypatch):
    monkeypatch.setattr(transport, "_is_done", lambda _entry: False)
    transport._campaigns[CID] = object()               # noqa: SLF001


def test_a_campaign_the_service_is_not_driving_reads_to_eof(transport):
    _campaign(transport.campaign_dir(CID).parent, {"controller.log": _stamp(1, "INFO", "ran")})
    chunk = transport.get_campaign_logs(CID)
    assert ([r.message for r in chunk.rows], chunk.eof, chunk.phases) == (["ran"], True, ["RUN"])
    assert transport.get_campaign_logs(CID, chunk.cursor).eof is True


def test_a_live_campaign_never_reads_to_eof(transport, monkeypatch):
    _campaign(transport.campaign_dir(CID).parent, {"controller.log": _stamp(1, "INFO", "ran")})
    _live(transport, monkeypatch)
    chunk = transport.get_campaign_logs(CID)
    assert ([r.message for r in chunk.rows], chunk.eof) == (["ran"], False)


def test_the_filters_reach_the_read_through_the_service(transport):
    _campaign(transport.campaign_dir(CID).parent, {
        "build.log": "#1 load\n",
        "controller.log": _stamp(1, "INFO", "ok") + _stamp(2, "ERROR", "bad")})
    chunk = transport.get_campaign_logs(CID, phase="run", min_level="error", grep="ba")
    assert [r.message for r in chunk.rows] == ["bad"]
    assert chunk.phases == ["BUILD", "RUN"]
    with pytest.raises(ValueError, match="unknown phase"):
        transport.get_campaign_logs(CID, phase="nope")


def test_a_line_written_through_the_campaign_log_handler_is_a_row_at_once(transport):
    """Liveness the stream depends on: a record written through the real handler is read
    without closing it, because the ``FileHandler`` flushes per record."""
    import logging

    from robovast.client.logging_config import (add_campaign_log_handler,
                                                remove_campaign_log_handler)
    log_path = transport.campaign_dir(CID) / EXECUTION_DIR / "controller.log"
    robovast_logger = logging.getLogger("robovast")
    prev_level = robovast_logger.level
    robovast_logger.setLevel(logging.INFO)  # runtime configures this; a bare test doesn't
    handler = add_campaign_log_handler(str(log_path))
    try:
        logging.getLogger("robovast.execution.controller").info("live line one")
        assert [r.message for r in transport.get_campaign_logs(CID).rows] == ["live line one"]
        logging.getLogger("robovast.execution.controller").warning("live line two")
        rows = transport.get_campaign_logs(CID).rows
        assert [(r.level, r.message) for r in rows] == [("INFO", "live line one"),
                                                        ("WARNING", "live line two")]
    finally:
        remove_campaign_log_handler(handler)
        robovast_logger.setLevel(prev_level)
