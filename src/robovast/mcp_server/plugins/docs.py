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

Pages are also available as ``docs://<name>`` MCP resources. ``search_docs`` covers all
three operations (list / search / read one) because not every client surfaces resources,
and a caller that could search but not read had to be told to fetch a URI its client may
have no way to fetch.

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


_DIRECTIVE_RE = re.compile(
    r"^\.\.\s+(auto(?:module|class|function)|literalinclude)::\s+(\S+)\s*$",
    re.MULTILINE,
)

_MCP_TOOLS_RE = re.compile(
    r"^\.\.\s+mcp-tools::\s+(\S+)\s*$",
    re.MULTILINE,
)

# Inline interpreted-text roles like :doc:`how_to_run`, :ref:`label`,
# :repo_link:`configs/examples/growth_sim`, :func:`x`.  Rendered to plain text.
_INLINE_ROLE_RE = re.compile(r":[\w:+-]+:`([^`]+)`")


def _select_lines(lines: list[str], spec: str) -> list[str]:
    """Select 1-indexed, inclusive line ranges per a Sphinx ``:lines:`` spec.

    Supports single lines, ``a-b`` ranges, open-ended ``a-``/``-b``, and
    comma-separated combinations (e.g. ``"2-21,32-36"``).
    """
    result: list[str] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            if "-" in part:
                a, b = part.split("-", 1)
                start = int(a) if a.strip() else 1
                end = int(b) if b.strip() else len(lines)
            else:
                start = end = int(part)
        except ValueError:
            continue
        result.extend(lines[max(0, start - 1) : end])
    return result


def _render_literalinclude(rel_path: str, options: dict[str, str], base_dir: Path) -> str:
    """Embed a ``.. literalinclude::`` target as a fenced code block."""
    path = (base_dir / rel_path).resolve()
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as e:
        return f"*[literalinclude:: {rel_path} — could not read: {e}]*"
    spec = options.get("lines")
    if spec:
        lines = _select_lines(lines, spec)
    lang = options.get("language", "").strip()
    out: list[str] = []
    caption = options.get("caption")
    if caption:
        out += [f"**{caption}**", ""]
    out.append(f"```{lang}".rstrip())
    out += lines
    out.append("```")
    return "\n".join(out)


def _strip_inline_roles(text: str) -> str:
    """Turn ``:role:`content``` into plain text (``title <target>`` → title)."""
    def _repl(m: re.Match) -> str:
        content = m.group(1)
        titled = re.match(r"(.*?)\s*<[^>]+>\s*$", content)
        return titled.group(1) if titled else content

    return _INLINE_ROLE_RE.sub(_repl, text)


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


def _resolve_directives(text: str, base_dir: Path) -> str:
    """Resolve autodoc, ``literalinclude``, ``mcp-tools``, and inline roles.

    Produces self-contained plain text: autodoc directives are expanded from
    live objects, ``literalinclude`` targets are embedded as code blocks (so
    example snippets travel with the doc), tool listings are rendered, and
    cross-reference roles are reduced to their display text. *base_dir* is the
    directory the document lives in, used to resolve ``literalinclude`` paths.
    """
    def _replace_mcp_tools(m: re.Match) -> str:
        return _resolve_mcp_tools_directive(m.group(1)) + "\n"

    text = _MCP_TOOLS_RE.sub(_replace_mcp_tools, text)

    lines = text.splitlines(keepends=True)
    result: list[str] = []
    i = 0
    while i < len(lines):
        m = _DIRECTIVE_RE.match(lines[i])
        if not m:
            result.append(lines[i])
            i += 1
            continue

        directive, target = m.group(1), m.group(2)
        i += 1

        # Consume indented option lines (:members:, :lines:, :language:, …)
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

        if directive == "literalinclude":
            result.append(_render_literalinclude(target, options, base_dir) + "\n")
        else:
            result.append(_autodoc_to_rst(directive, target, options) + "\n")

    return _strip_inline_roles("".join(result))


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


def _extract_md_title(text: str) -> str | None:
    """Return the first Markdown ``# heading`` (or first non-empty line)."""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
        return stripped
    return None


def _collect_doc_sources(docs_dir: Path) -> dict[str, tuple[Path, str]]:
    """Discover documentation sources as ``name -> (path, kind)``.

    Generic and maintenance-free: every ``docs/*.rst`` (kind ``"rst"``) and
    ``docs/*.md`` page, the repository ``README.md``, and each top-level
    package README under ``src/*/README.md`` (all kind ``"md"``). ``index`` is
    skipped; ``.rst`` wins a name collision with ``.md``.
    """
    sources: dict[str, tuple[Path, str]] = {}
    for p in sorted(docs_dir.glob("*.rst")):
        if p.stem != "index":
            sources[p.stem] = (p, "rst")
    for p in sorted(docs_dir.glob("*.md")):
        sources.setdefault(p.stem, (p, "md"))

    repo_root = docs_dir.parent
    root_readme = repo_root / "README.md"
    if root_readme.is_file():
        sources["readme"] = (root_readme, "md")
    src_dir = repo_root / "src"
    if src_dir.is_dir():
        for p in sorted(src_dir.glob("*/README.md")):
            sources[f"readme-{p.parent.name}"] = (p, "md")
    return sources


# -- Module-level doc loading ------------------------------------------------

_docs_dir: Path | None = _find_docs_dir()

_doc_files: dict[str, Path] = {}
_doc_meta: dict[str, str] = {}
_doc_content: dict[str, str] = {}

if _docs_dir is not None:
    for _name, (_path, _kind) in _collect_doc_sources(_docs_dir).items():
        _text = _path.read_text(encoding="utf-8", errors="replace")
        _doc_files[_name] = _path
        if _kind == "rst":
            _doc_meta[_name] = _extract_title(_text) or _name
            _doc_content[_name] = _resolve_directives(_text, _path.parent)
        else:
            _doc_meta[_name] = _extract_md_title(_text) or _name
            _doc_content[_name] = _text


# -- Tool functions ----------------------------------------------------------


def _no_docs() -> dict:
    return {"error": "documentation directory not found; set ROBOVAST_DOCS_DIR to the "
                     "docs/ path."}


def search_docs(query: str = "", page: str = "") -> dict:
    """The RoboVAST documentation: list the pages, search them, or read one.

    Args:
        query: Case-insensitive search term. Returns matching excerpts with 2 lines of
            context, grouped by page.
        page: Read this page in full (a ``name`` from the listing).

    Returns:
        Listing (neither argument): ``{pages, total}`` of ``{name, title}``.
        Search: ``{results, total}`` of ``{page, title, matches}``.
        Page: ``{page, title, content}``. Or ``{error}``.
    """
    if not _doc_files:
        return _no_docs()

    if page:
        if page not in _doc_files:
            return {"error": f"unknown documentation page {page!r}; available: "
                             f"{', '.join(sorted(_doc_files))}"}
        return {"page": page, "title": _doc_meta[page], "content": _doc_content[page]}

    if not query:
        pages = [{"name": name, "title": _doc_meta[name]} for name in sorted(_doc_files)]
        return {"pages": pages, "total": len(pages)}

    results = []
    query_lower = query.lower()
    for name in sorted(_doc_files):
        lines = _doc_content[name].splitlines()
        matches = [{"line": i + 1,
                    "excerpt": "\n".join(lines[max(0, i - 2):min(len(lines), i + 3)])}
                   for i, line in enumerate(lines) if query_lower in line.lower()]
        if matches:
            results.append({"page": name, "title": _doc_meta[name], "matches": matches})
    return {"results": results, "total": len(results)}


# -- Plugin class ------------------------------------------------------------

_TOOLS = [
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
