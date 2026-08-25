# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``robovast-client`` talks to a service over HTTP, and that is all it may need.

The distribution exists because ``pip install robovast`` pulls ~88 distributions and
~290 MB to run four HTTP verbs. That saving is not a property of the code, it is a
property of the dependency closure, and a single ``import`` added in the wrong file
takes it away: a client that needs ``kubernetes`` is a client that cannot be installed
by the audience it was built for, on a laptop with no cluster access at all.

:mod:`test_client_needs_no_core` guards the other half — that no client module needs
``robovast`` itself. This one guards what is *outside* both: the third-party closure.
It is derived from the installed metadata rather than hand-listed, so a dependency that
``pydantic`` or ``requests`` grows later is admitted automatically, while one this
distribution grows is not.
"""

import pathlib
import subprocess
import sys
import tomllib

CLIENT_SRC = (pathlib.Path(__file__).resolve().parents[2]
              / "src" / "robovast_client" / "robovast")
CLIENT_PYPROJECT = CLIENT_SRC.parent / "pyproject.toml"

#: What ``robovast-client`` is allowed to require. Adding to this set is a deliberate
#: act with a cost -- see the note in the client's ``pyproject.toml``, which asks
#: whether the service could do the work instead.
ALLOWED_DEPENDENCIES = {"python", "pydantic", "click", "requests"}


def _client_modules():
    """Module names from the client tree, not hand-listed: a list written out by hand
    stops covering the module somebody adds next week."""
    for path in sorted(CLIENT_SRC.rglob("*.py")):
        rel = path.relative_to(CLIENT_SRC.parent).with_suffix("")
        parts = list(rel.parts)
        if parts[-1] == "__init__":
            parts.pop()
        yield ".".join(parts)


def test_the_declared_dependencies_are_only_the_http_three():
    declared = set(tomllib.loads(CLIENT_PYPROJECT.read_text(encoding="utf-8"))
                   ["tool"]["poetry"]["dependencies"])
    assert declared == ALLOWED_DEPENDENCIES, (
        f"robovast-client's dependencies changed to {sorted(declared)}. It is installed "
        "by people who have a service URL and nothing else; anything heavier than an "
        "HTTP client belongs behind that service.")


# Runs in a subprocess: the blocker below refuses most of the world, which pytest itself
# would walk into the moment it wrote a report.
_PROBE = '''
import importlib, sys

ALLOWED = set(sys.stdlib_module_names) | ALLOWED_THIRD_PARTY | {"robovast"}


def _closure(roots):
    """Top-level module names of *roots* and everything they require, from the installed
    metadata -- so a dependency of a dependency needs no maintenance here."""
    from importlib.metadata import distribution, packages_distributions
    seen, queue = set(), list(roots)
    while queue:
        name = queue.pop()
        key = name.lower().replace("-", "_")
        if key in seen:
            continue
        seen.add(key)
        try:
            requires = distribution(name).requires or []
        except Exception:
            continue
        for req in requires:
            if "extra ==" in req:      # an optional extra nobody asked for
                continue
            queue.append(req.split(";")[0].strip().split("[")[0]
                         .split("<")[0].split(">")[0].split("=")[0]
                         .split("!")[0].split("~")[0].split(" ")[0])
    owners = {}
    for module, dists in packages_distributions().items():
        for dist in dists:
            owners.setdefault(dist.lower().replace("-", "_"), set()).add(module)
    return {m for key in seen for m in owners.get(key, ())}


class _OnlyTheClosure:
    """Refuse any import from outside what a client-only install would have."""

    def find_spec(self, name, path=None, target=None):
        top = name.split(".")[0]
        if (top in ALLOWED or top.startswith("__editable__")
                or top.startswith("_sysconfigdata")):
            return None            # None means "not my business", i.e. allowed
        raise ImportError(
            f"{name} is outside robovast-client's dependency closure "
            f"(importing {IMPORTING[0]})")


ALLOWED |= _closure(["pydantic", "click", "requests"])
# pip's editable machinery, the venv bootstrap and CPython's own build-time config module
# (`sysconfig` loads it by a platform-dependent name) are plumbing, not dependencies.
ALLOWED |= {"_distutils_hack", "_virtualenv", "pkg_resources", "setuptools"}
IMPORTING = ["<none>"]
sys.meta_path.insert(0, _OnlyTheClosure())

failures = []
for module in MODULES:
    IMPORTING[0] = module
    try:
        importlib.import_module(module)
    except ImportError as exc:
        failures.append(f"{module}: {exc}")
sys.meta_path.pop(0)
for line in failures:
    print(line)
print("checked", len(MODULES), "module(s)")
'''


def test_the_module_list_is_not_empty():
    """A guard derived from a directory is worth nothing if the directory moved."""
    modules = list(_client_modules())
    assert len(modules) >= 10, f"only found {modules}; CLIENT_SRC is probably wrong"


def test_no_client_module_reaches_outside_that_closure():
    """``import kubernetes`` in any client module fails here, not on a user's laptop."""
    modules = sorted(_client_modules())
    script = f"ALLOWED_THIRD_PARTY = set()\nMODULES = {modules!r}\n" + _PROBE
    done = subprocess.run([sys.executable, "-c", script], capture_output=True,
                          text=True, check=False)
    assert done.returncode == 0, done.stdout + done.stderr
    offenders = [line for line in done.stdout.splitlines()
                 if not line.startswith("checked ")]
    assert not offenders, "\n".join(offenders)
    assert f"checked {len(modules)} module(s)" in done.stdout
