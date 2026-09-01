# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""No live predicate tests for ``_execution/data.db``, the file the index replaced.

That file is not written any more, so every test for it answers "no" forever -- and each
time, the consequence was silent rather than an error: archives labelled raw, ``Status
.postprocessed`` stuck at false, and an import re-postprocessing campaigns that arrived
complete. A predicate that can only be wrong is worse than a missing one, because it reads
as a check.

**Shape, not words.** Prose about the retired file is legitimate and often necessary -- a
comment saying *why* something no longer looks for it is the record of this whole class.
What is banned is the file's name in a *value*: a string literal, an f-string, or a
JS/TS regex, which is how a path is built, globbed, matched or stat-ed. Python docstrings
are excluded for that reason (they are prose that happens to be a string), and so are
comments, which never reach the AST at all.

The replacements are ``common.campaign_data.campaign_has_derived_data`` ("has
postprocessing finished?", from its provenance record) and
``results_processing.index_query.campaign_is_ingested`` ("are its rows queryable?").
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]

_TREES = ("src", "frontend/ui/src", "frontend/panel-kit/src", "container", "tools")

#: Deliberately retained occurrences, each with the reason it is not a stale predicate.
#: An entry here is a claim that the code *means* to talk about a legacy artifact; adding
#: one without that being true is how the defect comes back.
_ALLOWED = {
    # Reports a legacy per-campaign analysis DB found in an archive written before the
    # central index. It says "absent" is the ordinary answer, so it cannot mislead.
    "src/robovast/service/ingest.py": "ingest report: legacy per-campaign analysis DB",
}

_JS_SUFFIXES = (".ts", ".tsx", ".js", ".jsx")
_JS_COMMENT = re.compile(r"//[^\n]*|/\*.*?\*/", re.DOTALL)
#: A string or regex literal carrying the name. ``data\.db`` covers a JS regex, where the
#: dot is escaped.
_JS_LITERAL = re.compile(r"""(?:'[^'\n]*|"[^"\n]*|`[^`]*|/[^/\n]*)data\\?\.db""")


def _sources():
    for tree in _TREES:
        base = _ROOT / tree
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or "node_modules" in path.parts:
                continue
            if path.suffix == ".py" or path.suffix in _JS_SUFFIXES:
                yield path


def _python_literals(path: Path) -> list[str]:
    """Lines where ``data.db`` appears in a string that is not a docstring."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
    hits = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and "data.db" in node.value and id(node) not in docstrings):
            hits.append(f"line {node.lineno}: {node.value.strip()[:80]}")
    return hits


def _js_literals(path: Path) -> list[str]:
    text = _JS_COMMENT.sub("", path.read_text(encoding="utf-8"))
    return [f"literal: {m.group(0)[-60:]}" for m in _JS_LITERAL.finditer(text)]


def test_no_data_db_predicate_in_shipped_code():
    offenders = {}
    for path in _sources():
        rel = path.relative_to(_ROOT).as_posix()
        if rel in _ALLOWED:
            continue
        hits = (_python_literals(path) if path.suffix == ".py" else _js_literals(path))
        if hits:
            offenders[rel] = hits
    assert not offenders, (
        "``_execution/data.db`` is never written any more, so a literal naming it in code "
        "is a predicate that can only answer 'no':\n"
        + "\n".join(f"  {rel}: {'; '.join(hits)}" for rel, hits in sorted(offenders.items()))
        + "\nUse campaign_data.campaign_has_derived_data (postprocessing finished?) or "
          "index_query.campaign_is_ingested (rows queryable?). Prose about the retired "
          "file stays fine -- put it in a comment or a docstring.")
