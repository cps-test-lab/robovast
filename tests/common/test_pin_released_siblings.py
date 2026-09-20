# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The release rewrite turns every path dependency into a pin, or refuses.

A path dependency reaches a wheel's metadata as a direct reference, which an index rejects
-- and a rewrite that quietly matched nothing would hand that rejection straight back to
the upload. So the script is held to both halves: the manifest it leaves behind names the
released version and no path, and a path it does not know is a failure, not a pass.
"""

import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "tools" / "pin_released_siblings.py"
SIBLINGS = ("robovast-client", "robovast-nav", "robovast-sim-roqsim")


def _run(cwd):
    return subprocess.run([sys.executable, str(SCRIPT), "2.1.0rc1"],
                          cwd=cwd, capture_output=True, text=True, check=False)


def _dependencies(text):
    return text.split("[tool.poetry.dependencies]", 1)[1].split("\n[", 1)[0]


def test_the_real_manifest_comes_out_pinned_and_pathless(tmp_path):
    shutil.copy(REPO / "pyproject.toml", tmp_path / "pyproject.toml")
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    text = (tmp_path / "pyproject.toml").read_text()
    for name in SIBLINGS:
        assert f'{name} = {{version = "==2.1.0rc1"' in text, name
    assert 'path = "' not in _dependencies(text)


def test_a_sibling_that_is_not_a_path_dependency_is_refused(tmp_path):
    """Nothing to rewrite is the silent failure this exists to make loud."""
    text = (REPO / "pyproject.toml").read_text()
    text = text.replace('robovast-client = {path = "src/robovast_client"}',
                        'robovast-client = "^2.0.0"')
    (tmp_path / "pyproject.toml").write_text(text)
    result = _run(tmp_path)
    assert result.returncode == 1
    assert "robovast-client: expected one path dependency, rewrote 0" in result.stderr


def test_a_path_dependency_the_script_does_not_know_is_refused(tmp_path):
    text = (REPO / "pyproject.toml").read_text()
    text = text.replace("[tool.poetry.dependencies]\n",
                        '[tool.poetry.dependencies]\nrobovast-new = {path = "src/robovast_new"}\n')
    (tmp_path / "pyproject.toml").write_text(text)
    result = _run(tmp_path)
    assert result.returncode == 1
    assert "robovast-new" in result.stderr
