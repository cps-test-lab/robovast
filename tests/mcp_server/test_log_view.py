# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The shared log view every MCP log tool reads through: four controls, and nothing
left out silently."""

import pytest

from robovast.mcp_server.log_view import view_log

_TEXT = "\n".join([
    "robovast  | [WARN] [1785092240.1] [tf_bridge]: TF_OLD_DATA ignoring data at 1.5",
    "robovast  | [WARN] [1785092241.1] [tf_bridge]: TF_OLD_DATA ignoring data at 2.5",
    "robovast  | [WARN] [1785092242.1] [tf_bridge]: TF_OLD_DATA ignoring data at 3.5",
    "[ERROR] [1785092243.0] [controller_server]: Failed to make progress",
    "[INFO] [1785092244.0] [nav2]: goal reached",
]) + "\n"


# -- the line view -----------------------------------------------------------


def test_unfiltered_view_reports_every_line_and_hides_nothing():
    view = view_log(_TEXT)
    assert view["lines"] == view["lines_total"] == 5
    assert view["dropped"] == 0 and view["truncated"] is False


def test_tail_marks_that_earlier_lines_were_cut():
    """`truncated` is what tells a caller to page back; without it a tail reads as
    the whole log."""
    view = view_log(_TEXT, tail=2)
    assert view["lines"] == 2 and view["truncated"] is True
    assert "goal reached" in view["content"]


def test_tail_takes_the_last_n_of_what_matched_not_of_the_whole_log():
    view = view_log(_TEXT, grep="TF_OLD_DATA", tail=1)
    assert view["lines"] == 1 and "at 3.5" in view["content"]


def test_a_redundant_relay_prefix_is_collapsed_but_the_level_survives():
    view = view_log(_TEXT, grep="TF_OLD_DATA", tail=1)
    assert not view["content"].startswith("robovast  |")
    assert "[WARN]" in view["content"]


def test_a_trailing_newline_is_preserved_for_incremental_polling():
    """These tools are polled and appended chunk after chunk; losing the newline
    splices the last line of one poll onto the first of the next."""
    assert view_log(_TEXT)["content"].endswith("\n")
    assert not view_log("a\nb")["content"].endswith("\n")


# -- min_severity ------------------------------------------------------------


def test_min_severity_error_keeps_only_errors_and_counts_the_rest_as_dropped():
    view = view_log(_TEXT, min_severity="error")
    assert view["lines"] == 1 and "Failed to make progress" in view["content"]
    assert view["dropped"] == 4


def test_min_severity_warn_includes_errors_because_it_is_a_floor():
    view = view_log(_TEXT, min_severity="warn")
    assert view["lines"] == 4 and view["dropped"] == 1


def test_grep_and_min_severity_compose_and_dropped_covers_both():
    view = view_log(_TEXT, grep="controller_server", min_severity="error")
    assert view["lines"] == 1 and view["dropped"] == 4


def test_an_unknown_severity_is_refused_rather_than_ignored():
    """A silently ignored filter would read as "no severe lines in this log" — the
    opposite of the truth."""
    with pytest.raises(ValueError, match="unknown severity"):
        view_log(_TEXT, min_severity="critical")


def test_an_invalid_grep_is_refused_rather_than_ignored():
    with pytest.raises(ValueError, match="not a valid regular expression"):
        view_log(_TEXT, grep="[")


# -- summarize ---------------------------------------------------------------


def test_summarize_returns_counted_patterns_and_no_content():
    """The two shapes are one key apart; `content` must be absent rather than
    empty, which would read as "nothing matched"."""
    view = view_log(_TEXT, summarize=True)
    assert "content" not in view
    assert view["patterns_total"] == 3
    assert view["patterns"][0]["count"] == 3
    assert view["severity_counts"] == {"other": 1, "warn": 3, "error": 1}


def test_summarize_still_reports_the_line_accounting():
    view = view_log(_TEXT, grep="TF_OLD_DATA", summarize=True)
    assert view["lines"] == 3 and view["lines_total"] == 5 and view["dropped"] == 2


def test_summarize_counts_the_relayed_flood_as_one_pattern():
    """The incident shape: identical text, a different relay timestamp per line."""
    text = "\n".join(
        f"robovast  | [WARN] [17850922{i:05d}.1] [tf_bridge]: TF_OLD_DATA at {i}.5"
        for i in range(500))
    view = view_log(text, summarize=True)
    assert view["patterns_total"] == 1 and view["patterns"][0]["count"] == 500


def test_summarize_ignores_tail_because_a_summary_is_not_a_page():
    with_tail = view_log(_TEXT, summarize=True, tail=1)
    without = view_log(_TEXT, summarize=True)
    assert with_tail["patterns"] == without["patterns"]


def test_summarize_honours_top():
    view = view_log(_TEXT, summarize=True, top=1)
    assert len(view["patterns"]) == 1 and view["patterns_total"] == 3
