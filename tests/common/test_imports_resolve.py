# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Every ``from robovast... import <name>`` must name something that exists.

This repo imports inside functions on purpose and at scale -- to keep module import cheap,
and to let one distribution reach another only when it actually has to. The cost is that a
deferred import is not checked until it runs. A caller was once committed without its
callee, and the whole suite passed: nothing imports that module at test time, so the
``ImportError`` waited for a user on the cluster lane, survived two further commits, and
shipped in a release.

So the check is static rather than dynamic. Nothing here is imported -- both sides are read
as ASTs -- which is what lets it cover modules that need kubernetes, ROS or a GPU to import,
and those are exactly the ones the test suite never loads.

What it cannot see: a name a module creates at runtime (``globals()`` assignment, a
metaclass, ``setattr``). If that ever appears, the fix is to declare it, not to weaken this
-- a name that no reader can find is a name the next caller will get wrong too.
"""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"


def _module_files() -> dict:
    """Importable module name -> file, for every module in this repo.

    The distribution directory is not part of the module path: ``robovast_client`` holds a
    ``robovast`` package, so ``src/robovast_client/robovast/client/cli.py`` is imported as
    ``robovast.client.cli``. Getting that wrong would silently map nothing and pass.
    """
    out = {}
    for path in SRC.rglob("*.py"):
        parts = [p for p in path.relative_to(SRC).with_suffix("").parts if p != "__init__"]
        if len(parts) > 1 and parts[0].startswith("robovast_") and parts[1] == "robovast":
            parts = parts[1:]
        out[".".join(parts)] = path
    return out


def _names_defined(path: Path) -> set:
    """Every top-level name *path* provides, re-exports included.

    ``ast.walk`` rather than a scan of ``tree.body``: a name defined inside an ``if
    TYPE_CHECKING`` or a ``try/except ImportError`` is still a name a caller can import,
    and treating those as absent would make the check cry wolf on the repo's own idioms.
    """
    names = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, ast.alias):
            names.add((node.asname or node.name).split(".")[0])
    return names


def test_every_intra_repo_import_names_something_that_exists():
    modules = _module_files()
    cache, missing = {}, []
    for path in sorted(SRC.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            # Relative imports are skipped: resolving them needs the importing package,
            # and the repo writes intra-repo imports absolutely almost everywhere.
            if not isinstance(node, ast.ImportFrom) or node.level or not node.module:
                continue
            # Only our own tree. A third-party name is that package's business, and
            # reading its source is not this test's job.
            if not node.module.startswith("robovast") or node.module not in modules:
                continue
            target = modules[node.module]
            if target not in cache:
                cache[target] = _names_defined(target)
            for alias in node.names:
                if alias.name == "*" or alias.name in cache[target]:
                    continue
                # `from robovast.client import cli` imports a SUBMODULE, which no amount of
                # reading the package's __init__ would show as a name.
                if f"{node.module}.{alias.name}" in modules:
                    continue
                missing.append(
                    f"{path.relative_to(SRC)}:{node.lineno} imports "
                    f"{alias.name!r} from {node.module}, which does not define it")
    assert not missing, (
        "an import names something that does not exist. Deferred (in-function) imports are "
        "not exercised by the suite, so this would have surfaced as an ImportError in front "
        "of a user:\n  " + "\n  ".join(missing))
