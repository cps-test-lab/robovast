# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The log summarizer: a flood collapses to one counted pattern, and severity is
classified from the producer's own marker rather than a keyword guess."""

from robovast.common.log_summary import (DEFAULT_TOP, SEVERITIES, normalize,
                                         peel_prefixes, severity_of, severity_rank,
                                         summarize)

#: The incident this module exists for: a bridge rejecting TF wholesale, one warning
#: per tick, each carrying a different timestamp and stamp.
def _tf_flood(n: int) -> list[str]:
    return [
        f"robovast  | [WARN] [17850922{i:05d}.111622055] [tf_bridge]: TF_OLD_DATA "
        f"ignoring data from the past for frame base_link at time {i}.5 according "
        f"to authority /gz"
        for i in range(n)
    ]


# -- the flood collapses -----------------------------------------------------


def test_a_flood_of_one_message_is_one_pattern_with_its_count():
    """The whole point: 18226 lines of noise must cost one line, and the count is
    the finding — a severity grep returned 25 of these and read as ordinary noise."""
    result = summarize(_tf_flood(18226))
    assert result["patterns_total"] == 1
    assert result["patterns"][0]["count"] == 18226
    assert "TF_OLD_DATA" in result["patterns"][0]["pattern"]
    # Counts are over LINES, not groups: "1 distinct warning" is not the finding.
    assert result["severity_counts"]["warn"] == 18226


def test_the_example_keeps_a_pattern_actionable():
    """A normalised pattern says *what* repeats; without a raw line it cannot be
    acted on (the placeholders have eaten the actual frame and time)."""
    group = summarize(_tf_flood(3))["patterns"][0]
    assert group["example"] in _tf_flood(3)
    assert "<n>" in group["pattern"] and "<n>" not in group["example"]


def test_messages_differing_only_in_numbers_group_but_different_text_does_not():
    lines = [
        "[WARN] [1.0] [amcl]: No laser scan received (and thus no pose updates) for 10 seconds",
        "[WARN] [2.0] [amcl]: No laser scan received (and thus no pose updates) for 20 seconds",
        "[WARN] [3.0] [amcl]: Waiting for the map",
    ]
    result = summarize(lines)
    assert result["patterns_total"] == 2
    assert result["patterns"][0]["count"] == 2


def test_the_same_text_from_two_nodes_stays_two_findings():
    """Attribution is part of the identity: one node timing out is not the same
    problem as every node timing out."""
    lines = ["[ERROR] [1.0] [controller_server]: Timed out",
             "[ERROR] [1.0] [planner_server]: Timed out"]
    assert summarize(lines)["patterns_total"] == 2


def test_top_caps_the_list_but_never_the_reported_total():
    """A truncated list that claimed to be complete would read as "that is all
    that is wrong"."""
    lines = [f"[WARN] [1.0] [n{i}]: distinct message {i}" for i in range(30)]
    result = summarize(lines, top=5)
    assert len(result["patterns"]) == 5
    assert result["patterns_total"] == 30


def test_top_zero_returns_every_pattern():
    lines = [f"[WARN] [1.0] [n{i}]: distinct message {i}" for i in range(30)]
    assert len(summarize(lines, top=0)["patterns"]) == 30


def test_patterns_are_ordered_by_count_descending():
    lines = ["[WARN] [1.0] [a]: rare"] + ["[WARN] [1.0] [b]: common"] * 5
    counts = [g["count"] for g in summarize(lines)["patterns"]]
    assert counts == sorted(counts, reverse=True)


# -- severity ----------------------------------------------------------------


def test_the_lines_own_level_marker_wins_over_any_keyword():
    """An INFO line reporting a zero error count must not be counted as an error;
    the producer's own verdict outranks a keyword scan of its text."""
    assert severity_of("[INFO] [1.0] [nav2]: error_code: 0, goal reached") == "other"
    assert severity_of("[ERROR] [1.0] [nav2]: goal reached") == "error"
    assert severity_of("[FATAL] [1.0] [x]: down") == "error"
    assert severity_of("[WARNING] [1.0] [x]: hmm") == "warn"
    assert severity_of("[DEBUG] [1.0] [x]: error error error") == "other"


def test_a_relayed_lines_inner_level_is_the_authoritative_one():
    """The relay stamps INFO onto everything it forwards; the payload's own level is
    the real one, or every forwarded error would read as informational."""
    line = ("robovast  | [INFO] [1785092240.111622055] [scenario_execution_ros]: "
            "[ERROR] [1785092241.5] [amcl]: transform failure")
    assert severity_of(line) == "error"


def test_a_relayed_line_without_its_own_level_keeps_the_relays():
    line = "robovast  | [WARN] [1785092240.111622055] [tf_bridge]: TF_OLD_DATA"
    assert severity_of(line) == "warn"


#: A real line from ``basic-nav-gazebo-2026-08-08-23475581``'s ``system.log``: nav2's
#: nodes run inside a launch container, so their output reaches the log wearing the
#: scenario's relay stamp *and* the container's process tag before their own stamp.
_NAV2_ERROR = (
    "[INFO] [1786225808.065243406] [scenario_execution_ros]: "
    "[component_container_isolated-8] [ERROR] [1786225807.288617653] "
    "[lifecycle_manager_localization]: CRITICAL FAILURE: SERVER map_server IS DOWN")


def test_an_error_behind_a_launch_tag_is_an_error():
    """The regression this guards: nine such lines in one campaign — including a
    CRITICAL FAILURE — all classified as ``warn``, and ``severity_counts`` reported
    ``error: 0``. So ``min_severity='error'`` answered "no errors" for a run whose
    localization stack had died. Anything nav2 logs is shaped like this, because its
    nodes live in a component container."""
    assert severity_of(_NAV2_ERROR) == "error"


def test_the_innermost_producer_is_the_one_attributed():
    """Both prefixes name a producer; the inner one is the node that actually spoke.
    Attributing to the launch container instead would group every node in it as one."""
    parsed = peel_prefixes(_NAV2_ERROR)
    assert parsed.node == "lifecycle_manager_localization"
    assert parsed.level == "ERROR"
    assert parsed.wall_ts == 1786225807.288617653
    assert parsed.message == "CRITICAL FAILURE: SERVER map_server IS DOWN"


def test_a_critical_line_is_an_error():
    """Python's stdlib emits CRITICAL, which rclpy never does -- so it was missing from the
    grammar, and a `logger.critical` line would have lost its timestamp *and* fallen through
    to the keyword scan. The same failure as the launch-tag bug above, by a different route."""
    line = "[CRITICAL] [1786266112.412529] [rst.engine]: physics diverged"
    assert severity_of(line) == "error"
    assert peel_prefixes(line).wall_ts == 1786266112.412529


def test_a_colour_escape_cannot_hide_a_stamp():
    """Every pattern here is anchored, so two invisible bytes in front of a marker would cost
    the line its timestamp while it still looked correct on a terminal. Producers really do
    this -- scenario-execution coloured its whole warning line, escape first."""
    line = "\x1b[33m[WARN] [100.5] [scenario_execution]: goal not reached\x1b[0m"
    parsed = peel_prefixes(line)
    assert (parsed.node, parsed.level, parsed.wall_ts) == ("scenario_execution", "WARN", 100.5)
    assert severity_of(line) == "warn"


def test_a_colour_escape_that_opens_the_message_is_left_alone():
    """The other half of the rule. gz writes `ESC[1;33mWarning [Utils.cc:132]`, where the
    escape is the producer's own colour and the log panel renders it. Skipping escapes
    unconditionally would trade a hidden stamp for a stripped colour."""
    line = "\x1b[1;33mWarning [Utils.cc:132]\x1b[0m no such entity"
    assert peel_prefixes(line).message == line, "the message keeps its colours"
    assert severity_of(line) == "warn"


def test_a_line_wearing_no_marker_reports_none_rather_than_guessing():
    parsed = peel_prefixes("Entrypoint script initialized")
    assert (parsed.node, parsed.level, parsed.wall_ts) == ("", "", None)
    assert parsed.message == "Entrypoint script initialized"


def test_an_unmarked_line_is_classified_by_the_published_pattern_as_warn():
    """Never `error`: with no marker there is nothing to separate warn from error,
    and inventing that distinction would report errors the log never claimed."""
    assert severity_of("Traceback (most recent call last):") == "warn"
    assert severity_of("connection refused") == "warn"
    assert severity_of("waiting for the simulator") == "other"


def test_severity_rank_orders_and_refuses_an_unknown_name():
    assert severity_rank("other") < severity_rank("warn") < severity_rank("error")
    assert list(SEVERITIES) == ["other", "warn", "error"]
    try:
        severity_rank("critical")
    except ValueError as e:
        assert "critical" in str(e) and "other, warn, error" in str(e)
    else:
        raise AssertionError("an unknown severity must not be silently accepted")


def test_a_group_takes_the_severity_of_its_worst_line():
    """Same normalised shape, escalating level: reporting the first one seen would
    hide that this message became an error."""
    lines = ["[WARN] [1.0] [x]: transform failure at 1.0",
             "[ERROR] [2.0] [x]: transform failure at 2.0"]
    groups = summarize(lines)["patterns"]
    assert len(groups) == 1 and groups[0]["severity"] == "error"


# -- normalisation -----------------------------------------------------------


def test_ids_and_coordinates_become_placeholders():
    key = normalize("[INFO] [1.0] [x]: pose 1.25 -3.5 at 0xdeadbeef "
                    "550e8400-e29b-41d4-a716-446655440000")
    assert key == "[x] pose <n> <n> at <hex> <uuid>"


def test_a_uuid_is_not_shredded_by_the_digit_rule():
    assert normalize("id 550e8400-e29b-41d4-a716-446655440000") == "id <uuid>"


def test_a_long_message_is_truncated_and_says_so():
    key = normalize("[INFO] [1.0] [x]: " + "y" * 400)
    assert key.endswith("…") and len(key) < 400


def test_empty_input_summarizes_to_nothing_rather_than_failing():
    result = summarize([])
    assert result["patterns"] == [] and result["patterns_total"] == 0
    assert result["severity_counts"] == {s: 0 for s in SEVERITIES}


def test_default_top_is_a_summary_not_a_log():
    assert 0 < DEFAULT_TOP <= 50
