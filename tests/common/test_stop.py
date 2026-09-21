# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Waiting and running subprocesses so a stop lands while they are still going.

The shared primitives every layer below the execution one stops through. What they must
hold is the same three things wherever they are used: a stopped subprocess dies *with
everything it started*, one nobody stops is untouched, and the ordinary path -- no
predicate at all -- costs nothing.
"""

import os
import signal
import subprocess
import threading
import time

import pytest

from robovast.common.errors import CampaignStopped
from robovast.common.stop import (run_watching_stop, sleep_unless_stopped, terminate_group,
                                  watch_stop)


def _gone(pid, timeout=5.0):
    """Whether *pid* is gone within *timeout*, allowing for the reaping that follows a kill."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            return True
        time.sleep(0.05)
    return False


def test_a_cancelled_step_takes_the_whole_process_group_with_it():
    """The group, not the process.

    A wrapper script waits on a foreground child, and bash defers a trap until that child
    returns -- so signalling the script alone would sit unhandled for exactly as long as
    the work being cancelled, and the container would keep going. The shape is reproduced
    here with a shell whose own child outlives it: killing the group is what reaches that
    child.
    """
    process = subprocess.Popen(  # noqa: S603
        ["bash", "-c", "sleep 30 & echo $!; wait"],  # noqa: S607
        stdout=subprocess.PIPE, text=True, start_new_session=True)
    grandchild = int(process.stdout.readline())

    with watch_stop(lambda: True, process, poll=0.05):
        process.wait(timeout=20)

    assert process.returncode != 0          # signalled, not a clean exit
    assert _gone(grandchild)                # ...and so was the child it was waiting on


def test_a_step_nobody_cancels_runs_to_its_own_end():
    """The watch must not be able to end work nobody stopped."""
    process = subprocess.Popen(["bash", "-c", "exit 7"],  # noqa: S603,S607
                               start_new_session=True)
    with watch_stop(lambda: False, process, poll=0.05) as watch:
        process.wait(timeout=20)

    assert process.returncode == 7
    assert watch.stopped is False


def test_no_predicate_starts_no_watching_thread():
    """The ordinary path -- a CLI run, a campaign nobody stops -- pays nothing for this."""
    process = subprocess.Popen(["bash", "-c", "exit 0"],  # noqa: S603,S607
                               start_new_session=True)
    before = threading.active_count()
    with watch_stop(None, process):
        assert threading.active_count() == before
    process.wait(timeout=20)


def test_the_watch_says_it_was_the_one_that_ended_it():
    """A killed process and a failed one both exit non-zero; only the watch tells them apart."""
    process = subprocess.Popen(["bash", "-c", "sleep 30"],  # noqa: S603,S607
                               start_new_session=True)
    with watch_stop(lambda: True, process, poll=0.05) as watch:
        process.wait(timeout=20)

    assert watch.stopped is True


def test_terminate_group_escalates_past_a_process_that_ignores_the_term():
    """The grace is a bound, not a hope: work that will not go is killed."""
    process = subprocess.Popen(  # noqa: S603
        ["bash", "-c", "trap '' SIGTERM; sleep 30"],  # noqa: S607
        start_new_session=True)
    time.sleep(0.5)  # let bash install the trap before it is signalled

    terminate_group(process, grace_s=0.5)

    # The caller is what reaps it -- ``run_watching_stop`` waits on the process either
    # way -- so the kill is read off the exit status rather than off the process table.
    assert process.wait(timeout=5) == -signal.SIGKILL


def test_a_killed_process_is_not_reported_as_a_failure():
    """The stop is the answer: a caller's failure diagnosis never sees the exit code.

    Without this the step's own error handling reports "exited -15", which reads as a
    broken command and sends whoever finds it looking for a bug in a step that worked.
    """
    started = time.monotonic()
    with pytest.raises(CampaignStopped, match="composition stopped by request"):
        run_watching_stop(["bash", "-c", "sleep 30"],  # noqa: S607
                          should_stop=lambda: True,
                          stopped_reason="composition stopped by request",
                          poll=0.05)
    assert time.monotonic() - started < 10


def test_run_watching_stop_returns_the_exit_code_when_nothing_stopped_it():
    """The path every campaign takes: the caller diagnoses its own failure as before."""
    lines = []
    rc = run_watching_stop(["bash", "-c", "echo hello; exit 3"],  # noqa: S607
                           should_stop=lambda: False, stopped_reason="unused",
                           on_line=lines.append, poll=0.05)

    assert rc == 3
    assert lines == ["hello"]


def test_sleep_unless_stopped_ends_within_a_poll_of_the_flag():
    """A poll loop with no event to wait on still pays one slice, not the whole interval."""
    flag = {"stopped": False}
    threading.Timer(0.1, lambda: flag.update(stopped=True)).start()

    started = time.monotonic()
    assert sleep_unless_stopped(30, lambda: flag["stopped"], poll=0.05) is True
    assert time.monotonic() - started < 5


def test_sleep_unless_stopped_sleeps_its_interval_when_nobody_stops_it():
    started = time.monotonic()
    assert sleep_unless_stopped(0.2, lambda: False, poll=0.05) is False
    assert time.monotonic() - started >= 0.2
