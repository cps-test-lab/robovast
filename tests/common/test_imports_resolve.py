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

It runs twice: over the working tree, and over the tree **as committed**. The second is
not redundant. The first miss shipped precisely because the definition existed on disk and
had not been committed, so the working tree was consistent while ``HEAD`` was not -- and an
image is built from a commit, not from somebody's disk. That is also why the fix cannot be
"remember to run CI": CI checks out the commit and would have caught it, but the run that
matters is the one before the push.

What neither can see: a name a module creates at runtime (``globals()`` assignment, a
metaclass, ``setattr``). If that ever appears, the fix is to declare it, not to weaken this
-- a name that no reader can find is a name the next caller will get wrong too.
"""

import ast
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"


def _module_name(parts) -> str:
    """The importable name for a path's parts, relative to ``src/``.

    The distribution directory is not part of the module path: ``robovast_client`` holds a
    ``robovast`` package, so ``src/robovast_client/robovast/client/cli.py`` is imported as
    ``robovast.client.cli``. Getting this wrong would map nothing and pass silently.
    """
    parts = [p for p in parts if p != "__init__.py"]
    if parts:
        parts = [*parts[:-1], parts[-1].removesuffix(".py")]
    parts = [p for p in parts if p != "__init__"]
    if len(parts) > 1 and parts[0].startswith("robovast_") and parts[1] == "robovast":
        parts = parts[1:]
    return ".".join(parts)


def _names_in(source: str) -> set:
    """Every top-level name *source* provides, re-exports included.

    ``ast.walk`` rather than a scan of ``tree.body``: a name defined inside an ``if
    TYPE_CHECKING`` or a ``try/except ImportError`` is still a name a caller can import,
    and treating those as absent would make the check cry wolf on the repo's own idioms.
    """
    names = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, ast.alias):
            names.add((node.asname or node.name).split(".")[0])
    return names


def _unresolved(paths, read, module_of):
    """Every ``from robovast... import name`` in *paths* that names nothing.

    *read* and *module_of* are injected so the same walk serves a working tree and a git
    tree: the only difference between them is where bytes come from and how a path spells
    a module.
    """
    cache, missing = {}, []
    modules = {module_of(p): p for p in paths}
    for path in paths:
        for node in ast.walk(ast.parse(read(path))):
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
                cache[target] = _names_in(read(target))
            for alias in node.names:
                if alias.name == "*" or alias.name in cache[target]:
                    continue
                # `from robovast.client import cli` imports a SUBMODULE, which no amount of
                # reading the package's __init__ would show as a name.
                if f"{node.module}.{alias.name}" in modules:
                    continue
                missing.append(f"{path}:{node.lineno} imports {alias.name!r} from "
                               f"{node.module}, which does not define it")
    return missing


_ADVICE = (
    "an import names something that does not exist. Deferred (in-function) imports are not "
    "exercised by the suite, so this surfaces as an ImportError in front of a user")


def test_every_intra_repo_import_names_something_that_exists():
    paths = sorted(SRC.rglob("*.py"))
    missing = _unresolved(paths,
                          read=lambda p: p.read_text(),
                          module_of=lambda p: _module_name(p.relative_to(SRC).parts))
    assert not missing, _ADVICE + ":\n  " + "\n  ".join(missing)


def test_the_committed_tree_is_importable_too():
    """The same check over ``HEAD``, because an image is built from a commit.

    This is the one that would have caught it. A caller was committed while its callee sat
    uncommitted next to it: the working tree resolved, ``HEAD`` did not, and the image built
    from ``HEAD`` failed on a path no test loads.
    """
    listed = subprocess.run(["git", "ls-tree", "-r", "--name-only", "HEAD", "src/"],
                            cwd=REPO, capture_output=True, text=True, check=True).stdout
    paths = [p for p in listed.splitlines() if p.endswith(".py")]
    blobs = {}

    def read(path):
        if path not in blobs:
            blobs[path] = subprocess.run(["git", "show", f"HEAD:{path}"], cwd=REPO,
                                         capture_output=True, text=True, check=True).stdout
        return blobs[path]

    missing = _unresolved(
        paths, read=read,
        module_of=lambda p: _module_name(Path(p).relative_to("src").parts))
    assert not missing, (
        _ADVICE + ". These resolve on disk but not in HEAD, so a definition is sitting "
        "uncommitted beside the caller that needs it:\n  " + "\n  ".join(missing))
