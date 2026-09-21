# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""How the roqsim backend reads ``roqsim --version``.

The line is ``roqsim, version <v>, build <commit>[-dirty]``, or ``..., build not recorded
(<reason>)``; a roqsim that predates the build identity prints only ``roqsim, version <v>``. That
last one is recorded as absent, with the reason, never as a guessed build.
"""

import pytest

roqsim_backend = pytest.importorskip(
    "robovast_sim_roqsim.backend",
    reason="no simulator installed; robovast is standalone (`make venv` installs one)")

parse = roqsim_backend.parse_roqsim_version
SHA = "0123456789abcdef0123456789abcdef01234567"


def test_a_build_is_the_full_commit():
    assert parse(f"roqsim, version 0.1.0, build {SHA}\n") == {
        "version": "0.1.0", "build": SHA, "dirty": False}


def test_a_dirty_build_says_so():
    assert parse(f"roqsim, version 0.1.0, build {SHA}-dirty") == {
        "version": "0.1.0", "build": SHA, "dirty": True}


def test_an_image_that_predates_the_identity_is_absent_not_guessed():
    parsed = parse("roqsim, version 0.1.0\n")
    assert parsed["version"] == "0.1.0"
    assert parsed["build"] is None
    assert "predates the build identity" in parsed["absent"]


def test_a_build_the_image_could_not_record_carries_its_reason():
    parsed = parse("roqsim, version 0.1.0, build not recorded (no baked file here)")
    assert parsed["build"] is None
    assert parsed["absent"].endswith("no baked file here")


def test_lines_before_the_version_line_are_not_the_answer():
    parsed = parse(f"MUJOCO_GL=egl selected\nroqsim, version 0.1.0, build {SHA}\n")
    assert parsed["build"] == SHA


def test_output_with_no_version_line_is_absent():
    parsed = parse("Traceback (most recent call last): ...")
    assert parsed["build"] is None
    assert "no version line" in parsed["absent"]


def test_the_backend_names_the_command_and_parses_its_output():
    backend = roqsim_backend.RoqsimBackend()
    assert backend.VERSION_COMMAND == ("roqsim", "--version")
    assert backend.parse_version(f"roqsim, version 0.1.0, build {SHA}")["build"] == SHA
