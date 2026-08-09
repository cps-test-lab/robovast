# Copyright (C) 2026 Frederik Pasch
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions
# and limitations under the License.
#
# SPDX-License-Identifier: Apache-2.0

"""Where a run's trial ends, read from what scenario-execution said.

Everything a run says or records after its scenario reached a verdict is **shutdown**:
nodes being killed, lifecycle transitions failing because their peer is already gone, a
TF listener complaining about a publisher that has stopped. Those lines are warnings and
errors by the shared classifier's definition, so they colour a log view red and dominate
a severity search, while saying nothing about the trial.

This module is the one place that recognises the verdict in *text*. It has exactly two
callers, and they are the two situations in which a verdict can be found:

* :mod:`robovast.results_processing.postprocessing_plugins`, which **records** it into
  ``scenario_timestamps`` while merging a finished run. Every later reader -- the web
  UI's playback clock and log views, ``search_run_logs`` -- reads that table rather than
  matching text again, so there is one answer to "when did the trial end".
* :func:`robovast.mcp_server.log_view.view_log`, which reads a **live** stream where no
  postprocessed table exists yet.

The strings come from scenario-execution's own logger and nowhere else
(``scenario_execution_base.py``: ``on_scenario_shutdown`` for the success line,
``add_result`` for the failure line).
"""

import re

from robovast.common import log_summary

#: Every ``failure_message`` ``on_scenario_shutdown`` can be given, plus the default it
#: substitutes when a caller passes none. Enumerated rather than matched loosely: the
#: failure line is ``f"{name}: {failure_message} {failure_output}"``, and a pattern like
#: "a colon followed by anything" would call every ``rclcpp`` error a verdict.
FAILURE_MESSAGES = (
    "execution failed.",       # the default, substituted when no message is given
    "Aborted",                 # KeyboardInterrupt / SIGINT
    "Setup failed",
    "Simulation setup failed",
    "Simulation reset failed",
    "Simulation reset parameter mismatch",
    "Could not create scenario output directory",
    "Run failed",              # an exception in the tick loop
    "parsing failed",          # before any scenario exists, so the name is empty
)

#: ``Scenario '<name>' succeeded.`` — logged at INFO by ``on_scenario_shutdown``.
_SUCCEEDED_RE = re.compile(r"^Scenario '.*?' succeeded\.")

#: ``<name>: <failure_message> <failure_output>`` — logged at ERROR by ``add_result``.
#: The name is an OSC scenario identifier, possibly qualified, and may be empty (a parse
#: failure has no scenario yet). Matching it as an identifier rather than as "anything up
#: to a colon" is what makes the anchor bite: prose ending in the same words ("reporting
#: that test_scenario: execution failed.") contains spaces and is refused. The output is
#: a multi-line py-trees snapshot, so only the head of the message is matched.
_FAILED_RE = re.compile(
    r"^[\w.-]*:\s(?:" + "|".join(re.escape(m) for m in FAILURE_MESSAGES) + r")(?:\s|$)")

#: ``Executing scenario '<name>'`` — what ends a suppressed span in a stream that
#: concatenates several runs.
_STARTED_RE = re.compile(r"^Executing scenario '")

#: Both loggers scenario-execution can run under: ``scenario_execution`` (plain) and
#: ``scenario_execution_ros``. Used as a prefix so neither has to be named twice.
_LOGGER_PREFIX = "scenario_execution"


def verdict_of(message: str, node: str = "") -> str:
    """``"succeeded"`` / ``"failed"`` / ``""`` for one log *message*.

    *message* is the line's payload with its prefixes already peeled -- a ``run_log``
    row's ``message`` column, or :func:`log_summary.peel_prefixes`'s ``message``. Both
    patterns are anchored at its start, which is what keeps a line that merely *quotes*
    a verdict ("waiting for Scenario 'x' succeeded.") from being read as one.

    *node* narrows it to scenario-execution's own logger when the caller knows it. An
    empty *node* means "unknown", not "wrong": a line with no stamp of its own still
    reaches here through a stream, and refusing it would lose the verdict on exactly the
    non-ROS runs this is meant to cover.
    """
    if node and not node.startswith(_LOGGER_PREFIX):
        return ""
    if _SUCCEEDED_RE.match(message):
        return "succeeded"
    if _FAILED_RE.match(message):
        return "failed"
    return ""


def is_scenario_start(message: str) -> bool:
    """Does *message* announce a scenario starting? See :data:`_STARTED_RE`."""
    return bool(_STARTED_RE.match(message))


def split_shutdown(lines: list) -> "tuple[list, int]":
    """Drop each shutdown span from *lines*, returning what is left and how many went.

    A span runs from the first **stamped** line after a verdict to the next
    ``Executing scenario`` (or the end of the stream). Two decisions shape that:

    * It starts at the first *stamped* line rather than at the verdict itself, because
      the failure line's ``failure_output`` -- the py-trees snapshot that explains the
      failure -- is appended to the same log record and arrives as unstamped
      continuation lines. Cutting at the verdict would delete the diagnosis of the
      failure that caused the cut. The cost is that unstamped output from a third party
      (a gz warning carries no stamp) survives if it lands before the next stamped line;
      that errs toward keeping, which is the safe direction here.
    * It *ends* at the next scenario start rather than at the end of the stream, because
      a campaign log concatenates runs locally (``get_campaign_log``'s ``run`` phase).
      Without that, run 1's verdict would swallow every run after it.
    """
    kept: list = []
    dropped = 0
    # "keep" -> a verdict has not been seen; "after" -> seen, still in its own record;
    # "drop" -> past the record, discarding until the next scenario starts.
    state = "keep"
    for line in lines:
        parsed = log_summary.peel_prefixes(line)
        if state == "after":
            if parsed.wall_ts is None:
                kept.append(line)  # still the verdict's own record
                continue
            state = "drop"
        if state == "drop":
            if is_scenario_start(parsed.message):
                state = "keep"
                kept.append(line)
            else:
                dropped += 1
            continue
        kept.append(line)
        if verdict_of(parsed.message, parsed.node):
            state = "after"
    return kept, dropped
