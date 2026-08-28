# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The entrypoints stamp their own lines, in the grammar the merged run log parses.

This is the producer half of the run log. Every other source in a container stamps itself --
rclpy does, scenario-execution's logger does -- and unstamped, these two scripts leave a
non-ROS run unplaceable in time: 46 lines, 0 stamped, on a measured run. A capture pipeline
would give them a timestamp from outside (a helper mounted at /config, a second copy of
every line on disk); stamping at the source needs none of it.

The tests run the *shipped* helper through a real bash and parse the result with the *shipped*
grammar, rather than asserting on a format string. A format that no longer matches the parser
is the failure mode that matters, and it cannot be seen by looking at either side alone.
"""

import subprocess

import pytest

from robovast.common.execution import _LOG_BLOCK, render_entrypoint
from robovast.common.log_summary import peel_prefixes, severity_of

SCRIPTS = ("entrypoint.sh", "secondary_entrypoint.sh")


def _run(script: str, env_prefix: str = "") -> list:
    """Lines printed by *script* appended to the shipped log helper."""
    out = subprocess.run(["bash", "-c", env_prefix + _LOG_BLOCK + "\n" + script],
                         capture_output=True, text=True, check=True)
    return out.stdout.splitlines()


def test_a_logged_line_parses_as_a_stamped_line():
    """Node, level and a real wall time -- the three things a row needs to be placed."""
    (line,) = _run('log "Running as UID: 1000, GID: 1000..."')
    parsed = peel_prefixes(line)
    assert parsed.node == "entrypoint"
    assert parsed.level == "INFO"
    assert parsed.wall_ts > 1_700_000_000, "a plausible epoch, not an uptime or a zero"
    assert parsed.message == "Running as UID: 1000, GID: 1000..."


@pytest.mark.parametrize("message,severity", [
    ("ERROR: Required tool(s) not found in container image:  mc", "error"),
    ("WARNING: post-run upload failed", "warn"),
    ("Setting up ROS2 environment...", "other"),
])
def test_the_level_comes_from_the_message_that_declares_one(message, severity):
    """A stamped level *wins* over the keyword scan, so hard-coding INFO here would downgrade
    every `log "ERROR: ..."` call site to routine output -- silently, in the panel's counts
    and in search_run_logs alike. The stamp has to carry what the text already says."""
    (line,) = _run(f'log "{message}"')
    assert severity_of(line) == severity


def test_the_stamp_survives_a_comma_decimal_locale():
    """EPOCHREALTIME is locale-formatted: under de_DE it yields `1786265636,970228`, which the
    grammar does not match. Nothing would report that -- the lines would simply lose their
    time -- so it is guarded in the helper and pinned here.

    Skipped rather than passed vacuously where the locale is not installed: `LC_ALL=de_DE` on a
    host without it falls back to C, and the test would prove nothing while looking green.
    """
    prefix = "export LC_ALL=de_DE.utf8 LC_NUMERIC=de_DE.utf8\n"
    probe = subprocess.run(["bash", "-c", prefix + 'echo "${EPOCHREALTIME}"'],
                           capture_output=True, text=True, check=True)
    if "," not in probe.stdout:
        pytest.skip("no comma-decimal locale here, so there is nothing to guard against")
    (line,) = _run('log "under a comma locale"', env_prefix=prefix)
    assert peel_prefixes(line).wall_ts is not None


def test_log_writes_to_stdout_only():
    """The redirect tees stdout into the log file, so a `tee -a` inside `log` -- which is what
    this had -- wrote every line to that file twice. It doubled a non-ROS run's whole log and
    made every count derived from it wrong by up to 2x."""
    code = "\n".join(l for l in _LOG_BLOCK.splitlines() if not l.lstrip().startswith("#"))
    assert "tee" not in code, "log() must not write the file itself; the redirect does"
    assert "LOG_FILE" not in code


@pytest.mark.parametrize("name", SCRIPTS)
def test_both_scripts_take_the_shared_helper(name):
    """One definition, because the two must emit the *same* format: a sidecar whose format
    drifted would not error, it would quietly lose its timestamps."""
    from importlib.resources import files
    body = files("robovast.execution.data").joinpath(name).read_text()
    assert "# @@LOG_BLOCK@@" in body
    assert "log() {" not in body, "the helper belongs to _LOG_BLOCK, not to either script"


def test_the_redirect_precedes_the_first_line_logged():
    """After the X11 block, everything above it -- the tool check, `Waiting for X
    socket...DONE` -- reaches the live log and never the durable file people read after a
    failure. Only `log` lines would be teed, and bare `echo`s in neither."""
    rendered = render_entrypoint(cluster=False)
    redirect = rendered.index("exec > >(stdbuf -oL tee")
    first_log = rendered.index('log "Running as UID')
    assert redirect < first_log
    assert "Waiting for X socket" in rendered[redirect:], \
        "the X11 setup must be captured, which is the point of moving the redirect up"
