# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The shared log view every MCP log tool reads through: five controls, and nothing
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


# -- hide_shutdown -----------------------------------------------------------

_SHUTDOWN_TEXT = "\n".join([
    "[INFO] [1785092240.0] [scenario_execution_ros]: Executing scenario 'trial'",
    "[INFO] [1785092241.0] [nav2]: goal reached",
    "[INFO] [1785092242.0] [scenario_execution_ros]: Scenario 'trial' succeeded.",
    "[ERROR] [1785092243.0] [amcl]: transform failure",
    "[ERROR] [1785092244.0] [controller_server]: Unable to start transition",
]) + "\n"


def test_shutdown_is_kept_by_default_because_the_primitive_stays_neutral():
    """`view_log` also trims `exec_in_container` and a build log, neither of which has
    a scenario. The tools that read a run's log opt in; this does not decide for them."""
    view = view_log(_SHUTDOWN_TEXT)
    assert view["lines"] == 5 and view["shutdown_dropped"] == 0


def test_hide_shutdown_stops_at_the_verdict_and_counts_what_it_dropped():
    view = view_log(_SHUTDOWN_TEXT, hide_shutdown=True)
    assert view["lines"] == 3 and view["shutdown_dropped"] == 2
    assert "succeeded." in view["content"] and "transform failure" not in view["content"]


def test_shutdown_dropped_is_reported_even_when_nothing_was_dropped():
    """Always present, `0` included: a key that appears only when it fired teaches a
    caller nothing on the call where it did not."""
    view = view_log("[INFO] [1785092240.0] [nav2]: goal reached", hide_shutdown=True)
    assert view["shutdown_dropped"] == 0


def test_the_two_exclusions_stay_separable_in_the_accounting():
    """`lines + dropped + shutdown_dropped == lines_total`. Folding the teardown into
    `dropped` would report a grep that matched everything it was shown as having cut it."""
    view = view_log(_SHUTDOWN_TEXT, hide_shutdown=True, grep="goal reached")
    assert view["lines_total"] == 5
    assert view["lines"] == 1 and view["dropped"] == 2 and view["shutdown_dropped"] == 2


def test_hide_shutdown_runs_before_tail_so_a_tail_ends_at_the_trial():
    """Applied after, a `tail` chasing a failure returns the end of the teardown --
    which is the noise the caller asked to be rid of."""
    view = view_log(_SHUTDOWN_TEXT, hide_shutdown=True, tail=1)
    assert "succeeded." in view["content"]


def test_hide_shutdown_also_reports_its_count_in_a_summary():
    view = view_log(_SHUTDOWN_TEXT, hide_shutdown=True, summarize=True)
    assert view["shutdown_dropped"] == 2 and view["lines"] == 3
