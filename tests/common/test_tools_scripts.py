# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The scripts under ``tools/``: each one loads in a dev venv, and the release rewrite takes only a version.

A script reached through a doc page or a Makefile target is run rarely and by hand, so an import
that names a module which has moved surfaces only when somebody needs it. Loading every one here
is the check its callers cannot make.
"""

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[2] / "tools"


@pytest.mark.parametrize("script", sorted(TOOLS.glob("*.py")), ids=lambda p: p.name)
def test_every_tool_script_loads(script, monkeypatch):
    """Each script imports what it names; running it is left to its own target or page."""
    monkeypatch.syspath_prepend(str(TOOLS))
    spec = importlib.util.spec_from_file_location(f"_tool_{script.stem}", script)
    spec.loader.exec_module(importlib.util.module_from_spec(spec))


@pytest.mark.parametrize("argument", ["--help", "", "latest", "v2.2.0"])
def test_pin_released_siblings_refuses_what_is_not_a_version(argument, tmp_path):
    """Anything but a version would be written into every sibling requirement."""
    manifest = tmp_path / "pyproject.toml"
    shutil.copy(TOOLS.parent / "pyproject.toml", manifest)
    before = manifest.read_text(encoding="utf-8")
    result = subprocess.run([sys.executable, str(TOOLS / "pin_released_siblings.py"), argument],
                            cwd=tmp_path, capture_output=True, text=True, check=False)
    assert result.returncode == 2, result.stdout + result.stderr
    assert manifest.read_text(encoding="utf-8") == before


@pytest.mark.parametrize("version", ["2.2.0", "2.1.0rc1", "2.2.0.post3"])
def test_pin_released_siblings_pins_a_version(version, tmp_path):
    """A release version replaces every sibling's path with that exact version."""
    manifest = tmp_path / "pyproject.toml"
    shutil.copy(TOOLS.parent / "pyproject.toml", manifest)
    result = subprocess.run([sys.executable, str(TOOLS / "pin_released_siblings.py"), version],
                            cwd=tmp_path, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    text = manifest.read_text(encoding="utf-8")
    assert f'robovast-client = {{version = "=={version}"}}' in text
