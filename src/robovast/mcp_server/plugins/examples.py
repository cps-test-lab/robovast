# Copyright (C) 2026 Frederik Pasch
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions
# and limitations under the License.
#
# SPDX-License-Identifier: Apache-2.0

"""MCP plugin that exposes the RoboVAST example projects.

Only examples that are **checked into git** are exposed. This is deliberate:
untracked example directories are work-in-progress that may not currently work,
and untracked files inside an example (``.cache/``, ``resolved/``,
``.robovast_temp_variation_config_*``, ``_transient/`` …) are generated
artifacts, not authored content. ``git ls-files`` is therefore the single,
maintenance-free source of truth for "authored vs generated" — the plugin needs
no per-example configuration and follows the examples automatically as they
change.

An *example* is any immediate subdirectory of ``configs/examples/`` that

* is not ``_``-prefixed (skips helper dirs such as ``_execution``), and
* contains at least one git-tracked ``.vast`` file.

The examples directory is resolved in this order:
1. ``ROBOVAST_EXAMPLES_DIR`` environment variable.
2. Walking up the directory tree from this file until a ``configs/examples/``
   folder is found (works in development / editable installs).
"""

import functools
import logging
import os
import subprocess
from pathlib import Path

import yaml
from fastmcp import FastMCP

from robovast.common.file_view import is_binary

logger = logging.getLogger(__name__)

#: Per-file line cap when inlining a file's content, to bound response size.
_MAX_FILE_LINES = 400


# -- Discovery ---------------------------------------------------------------


def _find_examples_dir() -> Path | None:
    env = os.environ.get("ROBOVAST_EXAMPLES_DIR")
    if env:
        p = Path(env)
        return p if p.is_dir() else None
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "configs" / "examples"
        if candidate.is_dir():
            return candidate
    return None


def _git_tracked_files(examples_dir: Path) -> list[str]:
    """Return git-tracked file paths under *examples_dir*, relative to it.

    Returns an empty list when *examples_dir* is not inside a git checkout;
    per the plugin's contract, no git checkout means no examples to expose (we
    never fall back to guessing which on-disk files are authored).
    """
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=examples_dir,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as e:
        logger.debug("git ls-files failed in %s: %s", examples_dir, e)
        return []
    return [p for p in out.split("\0") if p]


def _extract_description(example_dir: Path, files: list[str]) -> str:
    """Derive a human-readable description without any hardcoding.

    Priority: ``README.md`` first paragraph → primary ``.vast`` ``description``
    field → the directory name.
    """
    if "README.md" in files:
        try:
            text = (example_dir / "README.md").read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        para: list[str] = []
        for line in text.splitlines():
            stripped = line.strip().lstrip("#").strip()
            if stripped:
                para.append(stripped)
            elif para:
                break
        if para:
            return " ".join(para)

    for rel in sorted(f for f in files if f.endswith(".vast")):
        try:
            data = yaml.safe_load((example_dir / rel).read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(data, dict):
            desc = data.get("description") or (data.get("settings", {}) or {}).get("description")
            if isinstance(desc, str) and desc.strip():
                return desc.strip()

    return example_dir.name


# -- Example loading, on first use --------------------------------------------
#
# Deliberately not at import time. Building the catalog shells out to git and then
# opens every example's README and .vast, and this module is imported whenever the MCP
# is mounted -- so a `vast serve` paid for a catalog nobody had asked for. It also made
# the work observable from outside: a test that patched `subprocess.run` around anything
# that mounts the app caught this module's `git ls-files` and failed on the extra call,
# but only when nothing had imported the plugin earlier in the process. An import that
# does I/O turns test outcomes into a function of import order.


@functools.lru_cache(maxsize=1)
def _load_examples() -> tuple[Path | None, dict]:
    """``(examples_dir, {name: {"description", "files"}})``, computed once per process."""
    examples_dir = _find_examples_dir()
    examples: dict[str, dict] = {}
    if examples_dir is None:
        return None, examples

    grouped: dict[str, list[str]] = {}
    for rel in _git_tracked_files(examples_dir):
        name = rel.split("/", 1)[0]
        if name.startswith("_"):
            continue
        # relpath within the example dir
        inner = rel[len(name) + 1:]
        if inner:
            grouped.setdefault(name, []).append(inner)

    for name, files in grouped.items():
        if not any(f.endswith(".vast") for f in files):
            continue  # not an example (helper/support dir)
        files.sort()
        examples[name] = {
            "description": _extract_description(examples_dir / name, files),
            "files": files,
        }
    return examples_dir, examples


# -- Tool functions ----------------------------------------------------------


def get_example(name: str = "") -> dict:
    """Worked RoboVAST example projects: the catalog, or one project's files.

    Copy one into a workspace as the starting point for a new ``.vast``. Only
    git-committed examples are exposed; generated artifacts never are.

    Args:
        name: Example to retrieve, e.g. ``"basic_nav"``. Empty lists what is available.

    Returns:
        Listing: ``{examples, total}`` of ``{name, description, files}``.
        One example: ``{name, description, files}`` where each file is
        ``{path, content}`` — capped per file, with ``truncated``/``total_lines`` when
        it was cut, and a ``note`` instead of bytes for a binary. Or ``{error}``.
    """
    _examples_dir, _examples = _load_examples()
    if not _examples:
        return {"error": "no examples found; set ROBOVAST_EXAMPLES_DIR to a "
                         "configs/examples path inside a git checkout."}
    if not name:
        examples = [{"name": n, "description": _examples[n]["description"],
                     "files": _examples[n]["files"]} for n in sorted(_examples)]
        return {"examples": examples, "total": len(examples)}
    if name not in _examples:
        return {"error": f"unknown example {name!r}; available: "
                         f"{', '.join(sorted(_examples))}"}

    assert _examples_dir is not None  # guaranteed when _examples is non-empty
    base = _examples_dir / name
    files = []
    for rel in _examples[name]["files"]:
        path = base / rel
        entry: dict = {"path": rel}
        if is_binary(path):
            entry["note"] = "Binary file — content omitted."
        else:
            all_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            entry["content"] = "\n".join(all_lines[:_MAX_FILE_LINES])
            if len(all_lines) > _MAX_FILE_LINES:
                entry["truncated"] = True
                entry["total_lines"] = len(all_lines)
        files.append(entry)

    return {
        "name": name,
        "description": _examples[name]["description"],
        "files": files,
    }


# -- Plugin class ------------------------------------------------------------

_TOOLS = [
    get_example,
]


class ExamplesPlugin:
    """Expose git-tracked ``configs/examples/`` projects as MCP tools."""

    name = "examples"

    def register(self, mcp: FastMCP) -> None:
        """Register the example tools with the MCP server."""
        for fn in _TOOLS:
            mcp.tool()(fn)
