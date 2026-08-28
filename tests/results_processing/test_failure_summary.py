# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The one line a failed postprocessing step puts in the campaign's status.

That line is the whole of what a reader gets without opening a log: it is what
``get_campaign_status`` returns, what the MCP hands an agent, and what the web UI shows on
the card. The message's *first* line will not do: for the commonest failure it is the exit
status -- ``rosbags_process failed with exit code 1`` -- and says nothing about what to fix.
"""

from robovast.results_processing.postprocessing import _failure_summary


def test_the_cause_travels_with_the_exit_status():
    """Verbatim from nav2-baseline-pilot-navtopose-2026-08-13-00050125, which reported only
    its exit code while the line naming the bad handler stayed in the log."""
    message = (
        "rosbags_process failed with exit code 1\n"
        "Script directory: /src/robovast/results_processing/data\n"
        "Input directory: /results/nav2-baseline-pilot\n"
        "Detected ROS distribution: jazzy\n"
        "Error: unknown handler type(s): ['nav2bt_to_csv']. Available: ['to_csv', "
        "'tf_to_csv', 'nav2_bt_to_csv']\n"
    )
    summary = _failure_summary(message)
    assert summary.startswith("rosbags_process failed with exit code 1")
    assert "unknown handler type(s): ['nav2bt_to_csv']" in summary
    # One line: the field is read in a list and a tooltip.
    assert "\n" not in summary


def test_a_traceback_reports_its_exception_not_its_first_frame():
    """The last cause-shaped line, not the first: a traceback ENDS with what went wrong."""
    message = (
        "Plugin 'x' execution error\n"
        "Traceback (most recent call last):\n"
        '  File "a.py", line 1, in <module>\n'
        "ValueError: no such column 'objective'\n"
    )
    assert _failure_summary(message).endswith("ValueError: no such column 'objective'")


def test_a_message_with_no_cause_line_is_left_alone():
    # Nothing to add is not a reason to invent something; the first line stands.
    assert _failure_summary("nav2_bt_tree found no bt_xml") == "nav2_bt_tree found no bt_xml"
    assert _failure_summary("") == "failed (no output)"


def test_a_long_cause_is_truncated_rather_than_dropped():
    # A truncated cause still names the thing that went wrong; an exit code never does.
    message = "step failed with exit code 2\nError: " + "x" * 500
    summary = _failure_summary(message)
    assert summary.startswith("step failed with exit code 2 — Error: xxx")
    assert summary.endswith("…")
    assert len(summary) < 400
