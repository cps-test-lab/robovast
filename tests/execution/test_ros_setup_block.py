# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""One definition of the run's ROS environment, and it reaches every place that needs it.

The block is what makes a run's own tools findable: everything colcon-built in these images --
``scenario_execution`` first among them -- is importable only after ``/opt/ros`` and
``/ws/install`` are sourced, and the images deliberately put that in no shell rc. So a reader
that skipped it answered "No module named 'scenario_execution'" in every image ever built, and
rebuilding the image could not fix it.

It lived in three hand-written copies. They had already drifted -- one was missing the ``--``
that stops ROS's setup from reading the caller's positional parameters -- which is the drift
this collapses. Both directions are tested here: the shipped block through a real bash, and the
substitution into the scripts that carry it, because a marker nobody replaced is a script with
no ROS setup in it at all and nothing else would report that.
"""

import subprocess

import pytest

from robovast.common.execution import (ROS_SETUP_BLOCK, in_run_env, render_entrypoint)


def _run(script: str) -> subprocess.CompletedProcess:
    """The shipped block, then *script*, through a real bash under ``set -e`` as a run has it."""
    return subprocess.run(["bash", "-c", "set -e\n" + ROS_SETUP_BLOCK + "\n" + script],
                          capture_output=True, text=True, check=False)


def test_no_ros_is_a_no_op_and_not_a_failure():
    """A campaign with no ROS in its image must run the command it was given, unchanged. Under
    ``set -e`` a guard that tested the file without testing the distro would abort the whole
    script on an image that never had ROS."""
    out = _run('unset ROS_DISTRO\necho reached')
    assert out.returncode == 0, out.stderr
    assert "reached" in out.stdout


def test_a_distro_whose_setup_is_absent_does_not_abort():
    """``ROS_DISTRO`` set with nothing behind it is a real state -- a base image that exports it
    without shipping the overlay -- and it must not take the run down before its command runs."""
    out = _run('ROS_DISTRO=nosuchdistro\necho reached')
    assert out.returncode == 0, out.stderr
    assert "reached" in out.stdout


def test_the_announcement_is_silent_unless_a_caller_supplies_one():
    """The entrypoints log the step into the run's log; a live exec has no ``log`` to call. The
    default has to be the shell's no-op, or the one line the copies differed in would be the
    reason they stayed copies."""
    assert "${ROS_SETUP_ANNOUNCE:-:}" in ROS_SETUP_BLOCK


def test_both_setups_are_sourced_with_a_terminator():
    """``--`` on both. Without it ROS's ``setup.bash`` reads the *caller's* positional parameters,
    so sourcing it from a script that has any -- a sidecar's command -- lets those arguments be
    interpreted by the ROS setup instead. The secondary entrypoint's own copy was missing it."""
    for setup in ('"/opt/ros/${ROS_DISTRO}/setup.bash" --', "/ws/install/setup.bash --"):
        assert setup in ROS_SETUP_BLOCK
    assert ROS_SETUP_BLOCK.index("/opt/ros") < ROS_SETUP_BLOCK.index("/ws/install"), \
        "the overlay is sourced on top of the distro, not under it"


def test_a_live_exec_runs_its_command_after_the_setup():
    """``in_run_env`` is the whole point on the diagnostic side: the command a caller would have
    typed, run where the run's own processes can be found."""
    argv = in_run_env("python3 -m scenario_execution.tree_state /out/cfg/1")
    # ``-c`` and not ``-lc``: the overlay is sourced explicitly right here, so a login shell adds
    # /etc/profile's opinion about PATH and buys nothing -- and "what does a login shell read"
    # was itself one of the guesses that made the original failure hard to place.
    assert argv[:2] == ["/bin/bash", "-c"]
    script = argv[2]
    assert script.index("/ws/install/setup.bash") < script.index("scenario_execution.tree_state")
    assert script.endswith("python3 -m scenario_execution.tree_state /out/cfg/1")


def test_a_compound_command_runs_in_full():
    """It used to be ``exec {command}``, so only the first simple command ran: everything after a
    ``;`` was dropped in silence and a braced group was a syntax error. A probe that answers with
    the output of the first third of what you typed is worse than one that refuses -- it looks
    like an answer, and the first report of this was read as "the command returned nothing"."""
    for command, expected in [("echo one; echo two", ["one", "two"]),
                              ("{ echo x; echo y; }", ["x", "y"]),
                              ("echo a && echo b", ["a", "b"])]:
        out = subprocess.run(in_run_env(command), capture_output=True, text=True,
                             env={"PATH": "/usr/bin:/bin"}, check=False)
        assert out.stdout.split() == expected, f"{command!r} ran only part of itself"


@pytest.mark.parametrize("cluster", [False, True])
def test_the_rendered_entrypoint_carries_the_block(cluster):
    """The marker substitution sits on every run's critical path, so it is asserted rather than
    assumed: an unreplaced marker is a comment, and a run whose entrypoint sourced nothing would
    fail much later, reporting a missing scenario executor instead of a missing substitution."""
    content = render_entrypoint(cluster=cluster)
    assert "@@ROS_SETUP_BLOCK@@" not in content
    assert "/ws/install/setup.bash --" in content
    assert "ROS_SETUP_ANNOUNCE=log" in content, "a run's own log must still say it happened"
    syntax = subprocess.run(["bash", "-n", "/dev/stdin"], input=content,
                            capture_output=True, text=True, check=False)
    assert syntax.returncode == 0, syntax.stderr
