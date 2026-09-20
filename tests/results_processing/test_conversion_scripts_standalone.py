# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The conversion scripts run in the campaign's own image, so they import nothing of robovast.

``results_processing/data`` is shipped as a directory into a container that holds the
system under test -- the only place the message definitions of the types a run recorded
exist -- and nothing else of this package. A module there may import the standard library,
what that image provides, and its siblings in the same directory. Anything more imports
fine on a developer machine and fails in the one place the script is run.

An allowlist rather than a check for ``robovast``: a third-party module the image does not
carry breaks the script the same way, and a denylist would pass it.
"""

import ast
import sys
from importlib.resources import files

import pytest

#: What a campaign's execution image is expected to provide beside the standard library: the
#: ROS 2 Python client and bag libraries, and the two packages every ROS 2 install pulls in.
IMAGE_MODULES = frozenset({
    "rosbag2_py", "rclpy", "rosidl_runtime_py", "tf2_py", "tf2_ros", "numpy", "yaml",
})

_DATA_DIR = files("robovast.results_processing.data")
_SCRIPTS = sorted(entry for entry in _DATA_DIR.iterdir()
                  if entry.is_file() and entry.name.endswith(".py"))
_SIBLINGS = frozenset(entry.name[:-3] for entry in _SCRIPTS)


def _imported_modules(tree: ast.AST):
    """``(line, top-level module)`` for every absolute import anywhere in *tree*."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.lineno, node.module.split(".")[0]


def test_the_directory_holds_the_conversion():
    assert "rosbags_process" in _SIBLINGS


@pytest.mark.parametrize("script", _SCRIPTS, ids=lambda entry: entry.name)
def test_script_imports_only_what_the_execution_image_has(script):
    tree = ast.parse(script.read_text(encoding="utf-8"), filename=script.name)
    allowed = set(sys.stdlib_module_names) | IMAGE_MODULES | _SIBLINGS
    foreign = sorted({f"{module} (line {line})" for line, module in _imported_modules(tree)
                      if module not in allowed})
    assert not foreign, (
        f"{script.name} imports {', '.join(foreign)}, which the campaign's execution image "
        "does not provide. The conversion scripts run there standalone; move shared code "
        "into a sibling module of this directory instead.")


def test_relative_imports_are_refused():
    """A relative import resolves only inside the package, never in the container."""
    for script in _SCRIPTS:
        tree = ast.parse(script.read_text(encoding="utf-8"), filename=script.name)
        relative = [node.lineno for node in ast.walk(tree)
                    if isinstance(node, ast.ImportFrom) and node.level > 0]
        assert not relative, f"{script.name}: relative import at line(s) {relative}"
