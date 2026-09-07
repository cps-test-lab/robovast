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

Only **authored** examples are exposed. This is deliberate: untracked example
directories are work-in-progress that may not currently work, and untracked files
inside an example (``.cache/``, ``resolved/``,
``.robovast_temp_variation_config_*``, ``_transient/`` …) are generated
artifacts, not authored content. ``git ls-files`` answers "authored vs generated"
in a checkout — the plugin needs no per-example configuration and follows the
examples automatically as they change.

A container image is the case git cannot answer: the source is copied in and
``.git`` is not, so ``configs/examples/MANIFEST`` — the same list, committed and
kept honest by ``make check-examples-manifest`` — is read instead. The order is
git first: in a checkout the index is current by construction and a stale
manifest must never hide an example someone just added.

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
        if p.is_dir():
            return p
        logger.warning("ROBOVAST_EXAMPLES_DIR is set to %s, which is not a directory; "
                       "no examples will be served.", env)
        return None
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "configs" / "examples"
        if candidate.is_dir():
            return candidate
    return None


#: The committed file list, read where git cannot answer. Written by
#: ``tools/examples_manifest.py``; a file *about* the examples, so it is not one of them.
MANIFEST_NAME = "MANIFEST"


def _git_tracked_files(examples_dir: Path) -> list[str]:
    """Return git-tracked file paths under *examples_dir*, relative to it.

    Empty when *examples_dir* is not inside a git checkout — which is not the same
    as "no examples"; see :func:`_authored_files`.
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


def _manifest_files(examples_dir: Path) -> list[str]:
    """Return the file paths listed in *examples_dir*'s ``MANIFEST``, relative to it."""
    manifest = examples_dir / MANIFEST_NAME
    try:
        text = manifest.read_text(encoding="utf-8")
    except OSError as e:
        logger.debug("no manifest at %s: %s", manifest, e)
        return []
    return [line.strip() for line in text.splitlines()
            if line.strip() and not line.startswith("#")]


def _authored_files(examples_dir: Path) -> list[str]:
    """Return the authored file paths under *examples_dir*, relative to it.

    Two sources, in this order, because only one of them can answer at a time:

    1. ``git ls-files`` — a checkout, where the index is current by construction, so a
       manifest that had gone stale must not hide an example someone just added.
    2. ``MANIFEST`` — an image, where the source was copied in without ``.git``.

    Both empty is still no examples: the contract never falls back to guessing which
    on-disk files are authored, because the generated ones sit in the same directories.
    """
    tracked = _git_tracked_files(examples_dir)
    if tracked:
        return tracked
    listed = _manifest_files(examples_dir)
    if listed:
        logger.info("%s is not a git checkout; reading the example list from %s",
                    examples_dir, MANIFEST_NAME)
    return listed


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
    for rel in _authored_files(examples_dir):
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


def _no_examples_reason() -> str:
    """Why the catalog is empty, in terms of the thing to change.

    Three different deployments reach this line and the fix differs for each, so the
    reply names which one it is rather than sending everyone to the same variable.
    """
    examples_dir, _ = _load_examples()
    env = os.environ.get("ROBOVAST_EXAMPLES_DIR")
    if examples_dir is None:
        if env:
            return f"ROBOVAST_EXAMPLES_DIR is set to {env}, which is not a directory."
        return ("no examples directory found; set ROBOVAST_EXAMPLES_DIR to a "
                "configs/examples path.")
    if (examples_dir / MANIFEST_NAME).is_file():
        return (f"{examples_dir} is not a git checkout and its {MANIFEST_NAME} lists no "
                f"example holding a .vast file.")
    return (f"{examples_dir} is not a git checkout and carries no {MANIFEST_NAME}, so "
            f"which of its files are authored cannot be known. A build that copies the "
            f"examples in must copy the {MANIFEST_NAME} beside them.")


def get_example(name: str = "") -> dict:
    """Worked RoboVAST example projects: the catalog, or one project's files.

    Copy one into a workspace as the starting point for a new ``.vast``. Only
    authored examples are exposed; generated artifacts never are.

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
        return {"error": _no_examples_reason()}
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
        # Absence first, and named for what it is. The catalog is a list of authored
        # files -- the git index, or the manifest -- and either lists a file whether or
        # not this tree still holds it. ``is_binary`` answers "binary" for anything it
        # cannot open, so a file that is simply not there was reported as a binary asset
        # the caller should fetch some other way.
        if not path.is_file():
            entry["note"] = ("Listed as an authored file but not present here — "
                             "no content to read.")
        elif is_binary(path):
            entry["note"] = "Binary file — content omitted."
        else:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                entry["note"] = f"Could not be read: {e}."
                files.append(entry)
                continue
            all_lines = text.splitlines()
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
    """Expose the authored ``configs/examples/`` projects as MCP tools."""

    name = "examples"

    def register(self, mcp: FastMCP) -> None:
        """Register the example tools with the MCP server."""
        for fn in _TOOLS:
            mcp.tool()(fn)
