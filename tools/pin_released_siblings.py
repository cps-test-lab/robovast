#!/usr/bin/env python3
# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""Point robovast's sibling dependencies at the version being released, not at the tree.

A path dependency reaches the built metadata as a *direct reference*
(`robovast-client @ file:///home/...`), which an index refuses to accept and which would name
a directory on the build machine even if it did. The siblings are developed in this tree and
released from the same tag, so the version to require is the one being published.

Rewrites `pyproject.toml` in the current directory in place, and fails if a name stops
matching or if any path dependency is left: a rewrite that silently does nothing hands the
upload straight back to the bug. Used by the publish workflow and by the TestPyPI rehearsal
in the Makefile, so the two build the same wheel.
"""

import pathlib
import re
import sys

SIBLINGS = ("robovast-client", "robovast-data", "robovast-decode", "robovast-nav",
            "robovast-sim-roqsim")


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <version>", file=sys.stderr)
        return 2
    version = sys.argv[1]
    manifest = pathlib.Path("pyproject.toml")
    text = manifest.read_text(encoding="utf-8")

    for name in SIBLINGS:
        pattern = re.compile(
            rf'^(?P<head>{re.escape(name)} = \{{[^}}\n]*?)path = "[^"]+"', re.M)
        text, count = pattern.subn(rf'\g<head>version = "=={version}"', text)
        if count != 1:
            print(f"{name}: expected one path dependency, rewrote {count}", file=sys.stderr)
            return 1
        print(next(line for line in text.splitlines() if line.startswith(f"{name} = ")))

    dependencies = text.split("[tool.poetry.dependencies]", 1)[1].split("\n[", 1)[0]
    left = [line for line in dependencies.splitlines() if 'path = "' in line]
    if left:
        print("path dependencies this script does not know:\n" + "\n".join(left),
              file=sys.stderr)
        return 1

    manifest.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
