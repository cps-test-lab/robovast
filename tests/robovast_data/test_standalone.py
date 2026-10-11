# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""robovast-data is installed on its own, beside robovast-decode and nothing else of ours."""

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[2] / "src" / "robovast_data" / "robovast_data"
ALLOWED = ("robovast_data", "robovast_decode")


def test_it_imports_nothing_of_robovast_but_the_decoder():
    offending = []
    for path in sorted(PACKAGE.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            offending += [f"{path.name}: {name}" for name in names
                          if name.split(".")[0].startswith("robovast")
                          and name.split(".")[0] not in ALLOWED]
    assert not offending, offending
