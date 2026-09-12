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
        if p.is_dir():
            return p
        # A set-but-wrong path is a misconfiguration, not a request to search elsewhere:
        # falling through to the walk would serve some *other* docs directory under a
        # name the operator believes is theirs. Say so and serve nothing.
        logger.warning("ROBOVAST_DOCS_DIR is set to %s, which is not a directory; "
                       "no documentation will be served.", env)
        return None
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

    Generic and maintenance-free: every ``docs/*.rst`` (kind ``"roqsim"``) and
    ``docs/*.md`` page, the repository ``README.md``, and each top-level
    package README under ``src/*/README.md`` (all kind ``"md"``). ``index`` is
    skipped; ``.rst`` wins a name collision with ``.md``.
    """
    sources: dict[str, tuple[Path, str]] = {}
    for p in sorted(docs_dir.glob("*.rst")):
        if p.stem != "index":
            sources[p.stem] = (p, "roqsim")
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


#: Entry-point group a package uses to publish its own documentation to this corpus. Each entry
#: resolves to an object carrying a ``DOCS_DIR`` (the convention ``roqsim.models`` already uses for
#: ``MODELS_DIR``), and the entry-point NAME becomes the prefix its pages are served under.
#:
#: The substrate a campaign runs on is documented in its own repository, and robovast cannot reach
#: that by path without naming a sibling component -- which is exactly what a component that must
#: stay publishable on its own may not do. So the direction is inverted: a package that wants its
#: documentation served says so, and robovast names nobody.
DOCS_GROUP = "robovast.docs"

#: Additional corpora for a deployment whose packages predate the entry point, as
#: ``label=/path`` pairs separated by the platform path separator. A bare path takes its label
#: from the directory's parent. This is where cross-repository wiring belongs when it cannot be
#: a published name: in the configuration, not in either repository.
DOCS_EXTRA_ENV = "ROBOVAST_DOCS_EXTRA"


def _entry_point_doc_roots() -> list[tuple[str, Path]]:
    """``(label, docs_dir)`` for every package publishing docs through :data:`DOCS_GROUP`."""
    from importlib.metadata import entry_points

    roots: list[tuple[str, Path]] = []
    try:
        found = list(entry_points(group=DOCS_GROUP))
    except Exception as e:  # noqa: BLE001
        logger.warning("could not read the %s entry points: %s", DOCS_GROUP, e)
        return roots
    for ep in found:
        try:
            target = ep.load()
        except Exception as e:  # noqa: BLE001
            # One package's broken entry point must not cost every other package its docs.
            logger.warning("%s entry point %r could not be loaded: %s", DOCS_GROUP, ep.name, e)
            continue
        docs_dir = getattr(target, "DOCS_DIR", None)
        if docs_dir is None:
            logger.warning("%s entry point %r exposes no DOCS_DIR", DOCS_GROUP, ep.name)
            continue
        path = Path(docs_dir)
        if not path.is_dir():
            logger.warning("%s entry point %r points at %s, which is not a directory",
                           DOCS_GROUP, ep.name, path)
            continue
        roots.append((ep.name, path))
    return roots


def _env_doc_roots() -> list[tuple[str, Path]]:
    """``(label, docs_dir)`` for each entry of :data:`DOCS_EXTRA_ENV`."""
    raw = os.environ.get(DOCS_EXTRA_ENV, "")
    roots: list[tuple[str, Path]] = []
    for chunk in raw.split(os.pathsep):
        chunk = chunk.strip()
        if not chunk:
            continue
        label, _, spec = chunk.partition("=")
        if not spec:
            label, spec = "", chunk
        path = Path(spec)
        if not path.is_dir():
            # Set-but-wrong is a misconfiguration, as it is for ROBOVAST_DOCS_DIR: say so rather
            # than serving nothing under a name the operator believes is theirs.
            logger.warning("%s names %s, which is not a directory; skipping it",
                           DOCS_EXTRA_ENV, path)
            continue
        roots.append((label or path.parent.name, path))
    return roots


def _load_corpus(docs_dir: Path, prefix: str = "") -> dict[str, tuple[Path, str, str]]:
    """``name -> (path, kind, label)`` for one documentation root.

    *prefix* namespaces a secondary corpus, so ``architecture`` from the substrate is served as
    ``roqsim-architecture`` beside robovast's own -- both repositories have a page by that name,
    and silently letting one shadow the other would answer a question about one with the other.
    """
    out: dict[str, tuple[Path, str, str]] = {}
    for name, (path, kind) in _collect_doc_sources(docs_dir).items():
        out[f"{prefix}-{name}" if prefix else name] = (path, kind, prefix or "robovast")
    return out


# -- Module-level doc loading ------------------------------------------------

_docs_dir: Path | None = _find_docs_dir()

_doc_files: dict[str, Path] = {}
_doc_meta: dict[str, str] = {}
_doc_content: dict[str, str] = {}
#: page name -> which corpus it came from, so a listing says where an answer is from.
_doc_source: dict[str, str] = {}

_sources: dict[str, tuple[Path, str, str]] = {}
if _docs_dir is not None:
    _sources.update(_load_corpus(_docs_dir))
for _label, _root in _entry_point_doc_roots() + _env_doc_roots():
    # setdefault: robovast's own pages keep their unprefixed names, and the first registration
    # of a label wins, so a package registered twice cannot half-replace itself.
    for _key, _value in _load_corpus(_root, prefix=_label).items():
        _sources.setdefault(_key, _value)

for _name, (_path, _kind, _from) in _sources.items():
    _text = _path.read_text(encoding="utf-8", errors="replace")
    _doc_files[_name] = _path
    _doc_source[_name] = _from
    if _kind == "roqsim":
        _doc_meta[_name] = _extract_title(_text) or _name
        # Only robovast's own pages carry directives this resolver knows how to expand; another
        # repository's Sphinx extensions are its own, and are left as written rather than
        # half-rendered.
        _doc_content[_name] = (
            _resolve_directives(_text, _path.parent) if _from == "robovast" else _text)
    else:
        _doc_meta[_name] = _extract_md_title(_text) or _name
        _doc_content[_name] = _text


# -- Tool functions ----------------------------------------------------------


def _no_docs() -> dict:
    """The reply when no docs were loaded.

    It names which of the two ways that happened, because the fix differs: an unset
    variable is a deployment that never pointed at the docs, a set one is a path that
    does not hold them.
    """
    env = os.environ.get("ROBOVAST_DOCS_DIR")
    if env:
        return {"error": f"ROBOVAST_DOCS_DIR is set to {env}, which is not a readable "
                         "documentation directory."}
    return {"error": "documentation directory not found; set ROBOVAST_DOCS_DIR to the "
                     "docs/ path."}


#: Lines of context either side of a matching line.
_CONTEXT = 2

#: Excerpts returned per page by default. Bounded because a search reply is read by an
#: LLM, where every line costs context: a term as common as a product name matches
#: thousands of lines across the corpus, and returning all of them with context is
#: megabytes -- more than a client can carry, for a question that is answered by the
#: first few and a page name. ``limit=0`` still asks for every one.
_DEFAULT_EXCERPTS = 5


#: Matching lines above which a search says it is sampling. Not a cap -- the per-page
#: ``limit`` is the cap; this is the reply admitting that a word appearing this often is
#: answered by a page name rather than by more of its excerpts.
_COMMON_TERM_LINES = 100


def _excerpts(lines: list[str], hits: list[int], limit: int) -> tuple[list[dict], int]:
    """``(excerpts, windows_total)`` for the matching line indices *hits*.

    Neighbouring hits are merged into ONE excerpt when their context windows touch,
    rather than each producing its own: consecutive matches otherwise return the same
    five lines over and over, so a page's reply grew with how *clustered* its matches
    were rather than with how much it had to say. Each excerpt reports how many matching
    lines it covers, so nothing is hidden by the merge.
    """
    windows: list[list] = []
    for i in hits:
        start, end = max(0, i - _CONTEXT), min(len(lines), i + _CONTEXT + 1)
        if windows and start <= windows[-1][1]:
            windows[-1][1] = max(windows[-1][1], end)
            windows[-1][2] += 1
        else:
            windows.append([start, end, 1, i])
    kept = windows if limit <= 0 else windows[:limit]
    return ([{"line": first + 1, "matching_lines": count,
              "excerpt": "\n".join(lines[start:end])}
             for start, end, count, first in kept], len(windows))


def search_docs(query: str = "", page: str = "", limit: int = _DEFAULT_EXCERPTS) -> dict:
    """Documentation, ours and the substrate's: list, search, or read one page.

    Args:
        query: Case-insensitive search term. Returns matching excerpts with 2 lines of
            context, grouped by page; adjacent matches share one excerpt.
        page: Read this page in full (a ``name`` from the listing).
        limit: Maximum excerpts **per page** (``0`` = every one, which on a common term
            is megabytes). Narrow the term or read the page instead of raising this.

    Returns:
        Listing (neither argument): ``{pages, total}`` of ``{name, title}``.
        Search: ``{results, total, matching_lines_total, truncated}`` — each result
        ``{page, title, matches, matching_lines, excerpts_total, truncated}``, where
        ``matches`` are the excerpts returned and ``matching_lines`` is how many lines
        of that page matched at all. ``truncated`` marks a page whose excerpts were
        capped, so a narrowed read is never mistaken for the whole answer.
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
        pages = [{"name": name, "title": _doc_meta[name],
                  "source": _doc_source.get(name, "robovast")}
                 for name in sorted(_doc_files)]
        return {"pages": pages, "total": len(pages)}

    results = []
    matching_lines_total = 0
    truncated = False
    query_lower = query.lower()
    for name in sorted(_doc_files):
        lines = _doc_content[name].splitlines()
        hits = [i for i, line in enumerate(lines) if query_lower in line.lower()]
        if not hits:
            continue
        matches, excerpts_total = _excerpts(lines, hits, limit)
        matching_lines_total += len(hits)
        cut = len(matches) < excerpts_total
        truncated = truncated or cut
        results.append({"page": name, "title": _doc_meta[name], "matches": matches,
                        "matching_lines": len(hits), "excerpts_total": excerpts_total,
                        "truncated": cut})
    out = {"results": results, "total": len(results),
           "matching_lines_total": matching_lines_total, "truncated": truncated}
    if matching_lines_total > _COMMON_TERM_LINES:
        # A term this common is not answered by more excerpts of it. Say so, since the
        # reply otherwise reads as "here is what the docs say about X" when it is a
        # sample of the pages the word appears in.
        out["note"] = (
            f"{query!r} matches {matching_lines_total} lines across {len(results)} "
            "pages, so these excerpts are a sample: narrow the term, or read a page "
            "with search_docs(page=...)")
    return out


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

            ``search_docs()`` lists the page names, and reads one for a client with no
            way to fetch a resource URI.
            """
            if name not in _doc_files:
                available = ", ".join(sorted(_doc_files))
                raise ValueError(
                    f"Unknown documentation page {name!r}. Available: {available}"
                )
            return _doc_content[name]

        # Register each page as a static resource so clients can discover them
        # without calling the search_docs tool first.
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
