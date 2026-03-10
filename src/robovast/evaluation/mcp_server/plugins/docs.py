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
``docs://configuration``).  Use ``list_docs()`` to see all pages and
``search_docs(query)`` for keyword search across all pages.

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

from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)


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
    # Expand .. mcp-tools:: directives first (single-line, no options)
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


class DocsPlugin:
    """Expose ``docs/`` as MCP resources with search support."""

    name = "docs"

    def register(self, mcp: FastMCP, context=None) -> None:
        docs_dir = _find_docs_dir()

        if docs_dir is None:
            @mcp.tool()
            def docs_list() -> str:
                """List available RoboVAST documentation pages."""
                return (
                    "Documentation directory not found. "
                    "Set the ROBOVAST_DOCS_DIR environment variable to the docs/ path."
                )
            return

        # Collect all .rst files except index (which is just a TOC).
        doc_files: dict[str, Path] = {
            p.stem: p
            for p in sorted(docs_dir.glob("*.rst"))
            if p.stem != "index"
        }

        doc_meta: dict[str, str] = {}
        doc_content: dict[str, str] = {}
        for name, path in doc_files.items():
            text = path.read_text(encoding="utf-8")
            doc_meta[name] = _extract_title(text) or name
            doc_content[name] = _resolve_autodoc(text)

        # --- Resource: docs://{name} ---

        @mcp.resource("docs://{name}")
        def get_doc(name: str) -> str:
            """Retrieve a RoboVAST documentation page by name.

            Use list_docs() to discover available page names.
            """
            if name not in doc_files:
                available = ", ".join(sorted(doc_files))
                raise ValueError(
                    f"Unknown documentation page {name!r}. Available: {available}"
                )
            return doc_content[name]

        # --- Tool: list_docs ---

        @mcp.tool()  # pylint: disable=function-redefined
        def docs_list() -> list[dict]:
            """List all available RoboVAST documentation pages.

            Returns a list of records with ``name`` (use in docs://<name>) and
            ``title`` (human-readable heading from the document).
            """
            return [
                {"name": name, "title": doc_meta[name]}
                for name in sorted(doc_files)
            ]

        # --- Tool: docs_search ---

        @mcp.tool()
        def docs_search(query: str) -> list[dict]:
            """Search across all RoboVAST documentation pages for a keyword or phrase.

            Returns matching excerpts (with 2 lines of surrounding context) grouped
            by page.  Fetch the full page via the ``docs://<name>`` resource.

            Args:
                query: Case-insensitive search term.
            """
            results = []
            query_lower = query.lower()
            for name in doc_files:
                lines = doc_content[name].splitlines()
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
                            "title": doc_meta[name],
                            "matches": matches,
                        }
                    )
            return results
