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

"""MCP plugin that exposes the RoboVAST documentation as resources and tools.

Documentation pages are available as ``docs://<name>`` resources (e.g.
``docs://configuration``).  Use ``docs_list()`` to see all pages and
``docs_search(query)`` for keyword search across all pages.

The docs directory is resolved in this order:
1. ``ROBOVAST_DOCS_DIR`` environment variable.
2. Walking up the directory tree from this file until a ``docs/`` folder
   containing ``.rst`` files is found (works in development / editable installs).
"""

import importlib
import inspect
import logging
import os
import re
from pathlib import Path

from fastmcp import FastMCP

logger = logging.getLogger(__name__)


# -- Helpers -----------------------------------------------------------------


def _find_docs_dir() -> Path | None:
    env = os.environ.get("ROBOVAST_DOCS_DIR")
    if env:
        p = Path(env)
        return p if p.is_dir() else None
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "docs"
        if candidate.is_dir() and any(candidate.glob("*.rst")):
            return candidate
    return None


def _autodoc_to_rst(directive: str, target: str, options: dict[str, str]) -> str:
    """Resolve a single Sphinx autodoc directive to plain RST text."""
    try:
        if directive == "autofunction":
            mod_path, obj_name = target.rsplit(".", 1)
            mod = importlib.import_module(mod_path)
            func = getattr(mod, obj_name)
            try:
                sig = str(inspect.signature(func))
            except (ValueError, TypeError):
                sig = "(...)"
            doc = inspect.getdoc(func) or ""
            lines = [f"``{obj_name}{sig}``", ""]
            lines += doc.splitlines()
            return "\n".join(lines)

        if directive == "automodule":
            mod = importlib.import_module(target)
            members_str = options.get("members", "")
            if members_str:
                names = [n.strip() for n in members_str.split(",") if n.strip()]
            else:
                names = [n for n in dir(mod) if not n.startswith("_")]
            lines: list[str] = []
            mod_doc = inspect.getdoc(mod)
            if mod_doc:
                lines += mod_doc.splitlines() + [""]
            for name in names:
                obj = getattr(mod, name, None)
                if obj is None or not callable(obj):
                    continue
                try:
                    sig = str(inspect.signature(obj))
                except (ValueError, TypeError):
                    sig = "(...)"
                doc = inspect.getdoc(obj) or ""
                lines.append(f"``{name}{sig}``")
                lines += [f"    {l}" if l else "" for l in doc.splitlines()]
                lines.append("")
            return "\n".join(lines)

        if directive == "autoclass":
            mod_path, cls_name = target.rsplit(".", 1)
            mod = importlib.import_module(mod_path)
            cls = getattr(mod, cls_name)
            lines = [f"**{cls_name}**", ""]
            cls_doc = inspect.getdoc(cls)
            if cls_doc:
                lines += cls_doc.splitlines() + [""]
            members_str = options.get("members", "")
            if members_str:
                names = [n.strip() for n in members_str.split(",") if n.strip()]
            else:
                names = [
                    n
                    for n, _ in inspect.getmembers(cls, predicate=callable)
                    if not n.startswith("_")
                ]
            for name in names:
                obj = getattr(cls, name, None)
                if obj is None:
                    continue
                try:
                    sig = str(inspect.signature(obj))
                except (ValueError, TypeError):
                    sig = "(...)"
                doc = inspect.getdoc(obj) or ""
                lines.append(f"``{name}{sig}``")
                lines += [f"    {l}" if l else "" for l in doc.splitlines()]
                lines.append("")
            return "\n".join(lines)

    except Exception as e:
        logger.debug("autodoc resolution failed for %s %s: %s", directive, target, e)
        return f"*[{directive}:: {target} — could not resolve: {e}]*"

    return f"*[unsupported directive: {directive}]*"


_AUTODOC_RE = re.compile(
    r"^\.\.\s+(auto(?:module|class|function))::\s+(\S+)\s*$",
    re.MULTILINE,
)

_MCP_TOOLS_RE = re.compile(
    r"^\.\.\s+mcp-tools::\s+(\S+)\s*$",
    re.MULTILINE,
)


def _resolve_mcp_tools_directive(target: str) -> str:
    """Expand a ``.. mcp-tools::`` directive into a plain-text tool listing."""
    try:
        module_path, attr = target.rsplit(".", 1)
        mod = importlib.import_module(module_path)
        tools = getattr(mod, attr)
        lines = []
        for fn in tools:
            doc = (fn.__doc__ or "").strip().split("\n")[0]
            lines.append(f"- ``{fn.__name__}``: {doc}")
        return "\n".join(lines)
    except Exception as e:
        logger.debug("mcp-tools resolution failed for %s: %s", target, e)
        return f"*[mcp-tools:: {target} — could not resolve: {e}]*"


def _resolve_autodoc(text: str) -> str:
    """Replace Sphinx autodoc and mcp-tools directives with actual content."""
    def _replace_mcp_tools(m: re.Match) -> str:
        return _resolve_mcp_tools_directive(m.group(1)) + "\n"

    text = _MCP_TOOLS_RE.sub(_replace_mcp_tools, text)

    lines = text.splitlines(keepends=True)
    result: list[str] = []
    i = 0
    while i < len(lines):
        m = _AUTODOC_RE.match(lines[i])
        if not m:
            result.append(lines[i])
            i += 1
            continue

        directive, target = m.group(1), m.group(2)
        i += 1

        # Consume indented option lines (:members:, :undoc-members:, …)
        options: dict[str, str] = {}
        while i < len(lines):
            opt = re.match(r"[ \t]+:([\w-]+):\s*(.*)", lines[i])
            if opt:
                options[opt.group(1)] = opt.group(2).strip()
                i += 1
            elif lines[i].strip() == "" and i + 1 < len(lines) and re.match(r"[ \t]+:", lines[i + 1]):
                i += 1  # blank line between options
            else:
                break

        result.append(_autodoc_to_rst(directive, target, options) + "\n")

    return "".join(result)


def _extract_title(text: str) -> str | None:
    """Return the first RST section title found in *text*."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith(".."):
            continue
        if i + 1 < len(lines) and re.fullmatch(r"[=\-~^#*+]{2,}", lines[i + 1].strip()):
            if len(lines[i + 1].strip()) >= len(stripped):
                return stripped
    return None


# -- Module-level doc loading ------------------------------------------------

_docs_dir: Path | None = _find_docs_dir()

_doc_files: dict[str, Path] = {}
_doc_meta: dict[str, str] = {}
_doc_content: dict[str, str] = {}

if _docs_dir is not None:
    _doc_files = {
        p.stem: p
        for p in sorted(_docs_dir.glob("*.rst"))
        if p.stem != "index"
    }
    for _name, _path in _doc_files.items():
        _text = _path.read_text(encoding="utf-8")
        _doc_meta[_name] = _extract_title(_text) or _name
        _doc_content[_name] = _resolve_autodoc(_text)


# -- Tool functions ----------------------------------------------------------


def list_docs() -> list[dict] | str:
    """List all available RoboVAST documentation pages.

    Returns a list of records with ``name`` (use in docs://<name>) and
    ``title`` (human-readable heading from the document).
    """
    if not _doc_files:
        return (
            "Documentation directory not found. "
            "Set the ROBOVAST_DOCS_DIR environment variable to the docs/ path."
        )
    return [
        {"name": name, "title": _doc_meta[name]}
        for name in sorted(_doc_files)
    ]


def search_docs(query: str) -> list[dict]:
    """Search across all RoboVAST documentation pages for a keyword or phrase.

    Returns matching excerpts (with 2 lines of surrounding context) grouped
    by page.  Fetch the full page via the ``docs://<name>`` resource.

    Args:
        query: Case-insensitive search term.
    """
    results = []
    query_lower = query.lower()
    for name in _doc_files:
        lines = _doc_content[name].splitlines()
        matches = []
        for i, line in enumerate(lines):
            if query_lower in line.lower():
                start = max(0, i - 2)
                end = min(len(lines), i + 3)
                matches.append(
                    {
                        "line": i + 1,
                        "excerpt": "\n".join(lines[start:end]),
                    }
                )
        if matches:
            results.append(
                {
                    "page": name,
                    "title": _doc_meta[name],
                    "matches": matches,
                }
            )
    return results


# -- Plugin class ------------------------------------------------------------

_TOOLS = [
    list_docs,
    search_docs,
]


class DocsPlugin:
    """Expose ``docs/`` as MCP resources and tools."""

    name = "docs"

    def register(self, mcp: FastMCP) -> None:
        """Register all tool functions and the docs resource with the MCP server."""
        for fn in _TOOLS:
            mcp.tool()(fn)

        @mcp.resource("docs://{name}")
        def get_doc(name: str) -> str:
            """Retrieve a RoboVAST documentation page by name.

            Use list_docs() to discover available page names.
            """
            if name not in _doc_files:
                available = ", ".join(sorted(_doc_files))
                raise ValueError(
                    f"Unknown documentation page {name!r}. Available: {available}"
                )
            return _doc_content[name]

        # Register each page as a static resource so clients can discover them
        # without calling the list_docs tool first.
        for _page_name, _page_content in _doc_content.items():
            _uri = f"docs://{_page_name}"
            _title = _doc_meta.get(_page_name, _page_name)

            def _make_resource(content: str):
                def _resource_fn() -> str:
                    return content
                return _resource_fn

            mcp.resource(_uri, name=_title, description=f"RoboVAST docs: {_title}")(
                _make_resource(_page_content)
            )
