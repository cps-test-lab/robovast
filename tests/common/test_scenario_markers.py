# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Recognising where a run's trial ends, in the one place that reads it from text."""

import pytest

from robovast.common.scenario_markers import (FAILURE_MESSAGES, is_scenario_start,
                                              split_shutdown, verdict_of)

# -- the verdict -------------------------------------------------------------


def test_the_success_line_is_a_verdict():
    assert verdict_of("Scenario 'test_scenario' succeeded.") == "succeeded"


@pytest.mark.parametrize("failure_message", FAILURE_MESSAGES)
def test_every_failure_message_on_scenario_shutdown_can_give_is_a_verdict(failure_message):
    """Enumerated from `on_scenario_shutdown`'s callers. Matching only the default
    (`execution failed.`) is what the postprocessing matcher used to do, which left a
    run that ended `Aborted` or `Run failed` with no recorded end at all."""
    assert verdict_of(f"test_scenario: {failure_message} <snapshot>") == "failed"


def test_a_parse_failure_has_no_scenario_name_and_is_still_a_verdict():
    """`current_scenario` is None that early, so `add_result` logs a bare `: msg`."""
    assert verdict_of(": parsing failed some.osc:3:1") == "failed"


def test_the_failure_output_snapshot_rides_on_the_same_message():
    message = "test_scenario: execution failed. \n\n[-] test_scenario [*]\n    --> drive"
    assert verdict_of(message) == "failed"


def test_a_line_that_merely_quotes_a_verdict_is_not_one():
    """Both patterns are anchored at the start of the peeled message; a substring
    search would call every line that names the log line a verdict."""
    assert verdict_of("waiting until Scenario 'x' succeeded. is seen") == ""
    assert verdict_of("reporting that test_scenario: execution failed.") == ""


def test_an_ordinary_colon_line_is_not_a_verdict():
    assert verdict_of("amcl: transform failure for base_link") == ""
    assert verdict_of("Executing scenario 'test_scenario'") == ""


def test_a_verdict_from_another_node_is_refused_when_the_node_is_known():
    assert verdict_of("Scenario 'x' succeeded.", node="scenario_execution_ros") == "succeeded"
    assert verdict_of("Scenario 'x' succeeded.", node="scenario_execution") == "succeeded"
    assert verdict_of("Scenario 'x' succeeded.", node="amcl") == ""


def test_an_unknown_node_does_not_refuse_the_verdict():
    """A stream line with no stamp of its own reaches here with node="". Refusing it
    would lose the verdict on exactly the non-ROS runs this has to cover."""
    assert verdict_of("Scenario 'x' succeeded.", node="") == "succeeded"


def test_the_scenario_start_marker():
    assert is_scenario_start("Executing scenario 'test_scenario'")
    assert not is_scenario_start("Scenario 'test_scenario' succeeded.")


# -- the stream rule ---------------------------------------------------------


def _stamped(t, node, message):
    return f"[INFO] [{t}] [{node}]: {message}"


def test_everything_after_the_verdict_is_dropped_and_counted():
    lines = [
        _stamped(1.0, "scenario_execution_ros", "Executing scenario 'test_scenario'"),
        _stamped(2.0, "nav2", "goal reached"),
        _stamped(3.0, "scenario_execution_ros", "Scenario 'test_scenario' succeeded."),
        _stamped(4.0, "amcl", "transform failure"),
        _stamped(5.0, "controller_server", "Failed to shut down"),
    ]
    kept, dropped = split_shutdown(lines)
    assert dropped == 2
    assert kept == lines[:3]  # the verdict line itself stays


def test_nothing_is_dropped_from_a_stream_with_no_verdict():
    lines = [_stamped(1.0, "nav2", "goal reached"), "gz warning about a mesh"]
    assert split_shutdown(lines) == (lines, 0)


def test_the_failure_snapshot_survives_because_it_is_unstamped_continuation():
    """The py-trees snapshot explains the failure that caused the cut; cutting at the
    verdict line would delete exactly the diagnosis a reader came for."""
    lines = [
        _stamped(1.0, "scenario_execution_ros", "test_scenario: execution failed. "),
        "",
        "[-] test_scenario [*]",
        "    --> drive_to_goal",
        _stamped(2.0, "amcl", "transform failure"),
    ]
    kept, dropped = split_shutdown(lines)
    assert dropped == 1
    assert kept == lines[:4]


def test_a_stream_that_concatenates_runs_keeps_the_second_one():
    """A campaign log holds every run of the sweep locally. Ending the span at the end
    of the stream instead of at the next scenario start swallows run 2 entirely."""
    lines = [
        _stamped(1.0, "scenario_execution_ros", "Executing scenario 'trial'"),
        _stamped(2.0, "scenario_execution_ros", "Scenario 'trial' succeeded."),
        _stamped(3.0, "amcl", "transform failure"),
        _stamped(4.0, "scenario_execution_ros", "Executing scenario 'trial'"),
        _stamped(5.0, "nav2", "goal reached"),
        _stamped(6.0, "scenario_execution_ros", "Scenario 'trial' succeeded."),
        _stamped(7.0, "amcl", "transform failure"),
    ]
    kept, dropped = split_shutdown(lines)
    assert dropped == 2
    assert [line for i, line in enumerate(lines) if i not in (2, 6)] == kept


def test_a_verdict_relayed_through_a_container_tag_is_still_recognised():
    """A forwarded line wears the relay's stamp over the producer's own; `peel_prefixes`
    takes the innermost, which is the only one that says who reached the verdict."""
    lines = [
        "sut  | [INFO] [1785092243.0] [scenario_execution_ros]: Scenario 'x' succeeded.",
        "sut  | [ERROR] [1785092244.0] [amcl]: transform failure",
    ]
    kept, dropped = split_shutdown(lines)
    assert dropped == 1 and kept == lines[:1]
