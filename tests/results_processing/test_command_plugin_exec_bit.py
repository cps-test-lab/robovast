# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""The ``command`` postprocessing plugin must run a staged script that arrived
non-executable.

The cluster lane's staging initContainer fetches a campaign's whole tree with
per-file executable-bit restoration turned off (a deliberate cost decision for the
bulk fetch — see ``postprocess_stage.py``), so a script this plugin's ``get_files_to_copy``
staged into ``_config/`` can land there without its executable bit. This plugin is the
one caller that actually runs the file, so it is the layer that restores the bit.
"""

import os
import stat

from robovast.results_processing.postprocessing_plugins import Command


def test_a_non_executable_script_still_runs(tmp_path):
    script = tmp_path / "postprocess.sh"
    script.write_text("#!/bin/sh\necho ok\n")
    script.chmod(0o644)  # as a bulk fetch with executable_bits=False would land it
    results_dir = tmp_path / "campaign-x"
    results_dir.mkdir()

    ok, message = Command()(str(results_dir), str(tmp_path), script=str(script))

    assert ok, message
    assert "ok" in message


def test_the_restored_bit_only_adds_execute_permission(tmp_path):
    script = tmp_path / "postprocess.sh"
    script.write_text("#!/bin/sh\necho ok\n")
    script.chmod(0o640)  # owner rw, group r -- no execute anywhere
    results_dir = tmp_path / "campaign-x"
    results_dir.mkdir()

    ok, _ = Command()(str(results_dir), str(tmp_path), script=str(script))

    assert ok
    mode = stat.S_IMODE(os.stat(script).st_mode)
    assert mode == 0o751, "chmod must add x bits (0o111), not replace the mode outright"


def test_an_already_executable_script_is_left_alone(tmp_path):
    script = tmp_path / "postprocess.sh"
    script.write_text("#!/bin/sh\necho ok\n")
    script.chmod(0o750)
    results_dir = tmp_path / "campaign-x"
    results_dir.mkdir()

    ok, _ = Command()(str(results_dir), str(tmp_path), script=str(script))

    assert ok
    assert stat.S_IMODE(os.stat(script).st_mode) == 0o750
