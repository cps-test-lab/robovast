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

"""Execute an analysis notebook and export it to a self-contained HTML page.

The render core behind the ``robovast-service`` ``GET /campaigns/{id}/notebook``
endpoint, which the web UI's Results Explorer calls per selected tree node. Given a
notebook and the data directory of that node, it injects ``DATA_DIR`` (see
:ref:`evaluation-notebooks`), executes every cell, and exports the outputs to HTML with
the inputs hidden.

The result is cached per node: the cache lives in ``<data_dir>/.cache`` (so each
campaign/config/run node caches separately) and is keyed on the notebook's **content**
hash plus the injected variables — *not* the file's mtime/size. That matters for the
cluster service, whose ``fetch_campaign`` re-downloads the campaign on every request
and bumps every file's mtime; a content hash is what lets the cache actually hit.

This core is the happy path only: it executes, exports, caches, and returns the HTML,
or it raises. Partial-render-on-failure and the styled error page live in the desktop
widget, which wraps this function.
"""

import hashlib
import os
import re
from html import escape

import nbformat
from nbconvert import HTMLExporter
from nbconvert.preprocessors import ExecutePreprocessor

from robovast.common.file_cache2 import CacheKey, FileCache2

# Default per-cell execution timeout for the web path. Lower than the desktop's 600s:
# a web request shouldn't hold a threadpool slot for ten minutes.
DEFAULT_TIMEOUT = 300


def scrollbar_css(theme: str) -> str:
    """Theme-aware scrollbar + base-font CSS injected into the exported HTML head."""
    if theme == 'dark':
        track = "rgba(255, 255, 255, 0.08)"
        thumb = "rgba(255, 255, 255, 0.25)"
        thumb_hover = "rgba(255, 255, 255, 0.35)"
        color_scheme = "dark"
    else:
        track = "rgba(0, 0, 0, 0.05)"
        thumb = "rgba(0, 0, 0, 0.25)"
        thumb_hover = "rgba(0, 0, 0, 0.35)"
        color_scheme = "light"

    return f"""
<style id="robovast-scrollbar-style">
  html {{
    font-size: 14px;
    color-scheme: {color_scheme};
  }}
  :root {{
    --rv-scrollbar-track: {track};
    --rv-scrollbar-thumb: {thumb};
    --rv-scrollbar-thumb-hover: {thumb_hover};
  }}
  * {{
    scrollbar-width: thin; /* Firefox */
    scrollbar-color: var(--rv-scrollbar-thumb) var(--rv-scrollbar-track); /* Firefox */
  }}
  *::-webkit-scrollbar {{
    width: 12px;
    height: 12px;
  }}
  *::-webkit-scrollbar-track {{
    background: var(--rv-scrollbar-track);
  }}
  *::-webkit-scrollbar-thumb {{
    background-color: var(--rv-scrollbar-thumb);
    border-radius: 8px;
    border: 3px solid transparent;
    background-clip: content-box;
  }}
  *::-webkit-scrollbar-thumb:hover {{
    background-color: var(--rv-scrollbar-thumb-hover);
  }}
</style>
""".strip()


def message_page_html(message: str, theme: str = 'light') -> str:
    """A minimal, theme-aware standalone HTML page showing one centered message.

    Used by the web notebook endpoint to render an intentional "nothing to show" bail
    — a notebook's ``raise SystemExit("...")`` — as a neutral empty state in the
    Explorer iframe, rather than surfacing it as an execution error.
    """
    if theme == 'dark':
        bg, fg, color_scheme = "#111111", "rgba(255, 255, 255, 0.6)", "dark"
    else:
        bg, fg, color_scheme = "#ffffff", "rgba(0, 0, 0, 0.55)", "light"

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><style>
  html, body {{ height: 100%; margin: 0; }}
  body {{
    display: flex; align-items: center; justify-content: center;
    background: {bg}; color: {fg}; color-scheme: {color_scheme};
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    font-size: 14px; text-align: center; padding: 24px;
  }}
</style></head>
<body><div>{escape(message)}</div></body>
</html>"""


def inject_css_into_html_head(html_text: str, css_block: str) -> str:
    """Insert *css_block* just before ``</head>`` (or after ``<head>``) once."""
    if not html_text or not css_block:
        return html_text
    if 'id="robovast-scrollbar-style"' in html_text:
        return html_text

    head_close = re.search(r"</head\s*>", html_text, flags=re.IGNORECASE)
    if head_close:
        idx = head_close.start()
        return html_text[:idx] + "\n" + css_block + "\n" + html_text[idx:]

    head_open = re.search(r"<head(\s+[^>]*)?>", html_text, flags=re.IGNORECASE)
    if head_open:
        idx = head_open.end()
        return html_text[:idx] + "\n" + css_block + "\n" + html_text[idx:]

    return css_block + "\n" + html_text


def _apply_injections(notebook, data_dir: str, inject: dict | None) -> None:
    """Replace ``DATA_DIR = ...`` (required) and each ``inject`` var in code cells.

    Every ``<NAME> = ...`` assignment is rewritten in place with ``<NAME> = <repr>``,
    matching the desktop's contract. ``DATA_DIR`` must be present at least once.
    """
    injections = {"DATA_DIR": repr(os.path.abspath(data_dir))}
    for name, value in (inject or {}).items():
        injections[name] = repr(value)

    for var, replacement in injections.items():
        pattern = re.compile(rf'(?m)^(\s*){var}\s*=\s*.*$')
        count = 0
        for cell in notebook.cells:
            if cell.cell_type == 'code':
                cell.source, n = pattern.subn(rf'\1{var} = {replacement}', cell.source)
                count += n
        if var == "DATA_DIR" and count == 0:
            raise ValueError(
                f"Notebook {notebook.get('metadata', {}).get('name', '')} has no "
                "'DATA_DIR = ...' line to replace (see the evaluation-notebooks docs).")


# nbconvert's hierarchy, not ours
class _ProgressExecutePreprocessor(ExecutePreprocessor):  # pylint: disable=too-many-ancestors
    """``ExecutePreprocessor`` that reports per-cell progress and honours a cancel check.

    All three hooks are optional; without them this behaves like the plain preprocessor.
    The web endpoint takes *on_cell* only.

    *progress_cb* and *on_cell* report the same event in two shapes: a percentage plus a
    ready-made sentence for a status line, and raw ``(done, total)`` counts for a caller
    that draws its own bar. One callback serving both would mean parsing cell numbers back
    out of an English string. Only the second has a caller today — the first is kept
    because a long render still wants a sentence, and the cancel hook is what makes an
    abandoned render stop rather than run to completion.
    """

    def __init__(self, *args, progress_cb=None, on_cell=None, is_cancelled=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._progress_cb = progress_cb
        self._on_cell = on_cell
        self._is_cancelled = is_cancelled
        self._total = 1

    # nbconvert's documented signature
    def preprocess(self, nb, resources, km=None):  # pylint: disable=signature-differs
        self._total = max(1, len(nb.cells))
        return super().preprocess(nb, resources, km=km)

    def preprocess_cell(self, cell, resources, index):
        if self._is_cancelled and self._is_cancelled():
            raise RuntimeError("Notebook execution cancelled.")
        if self._progress_cb:
            # Scale cell progress into a 20..80% band (the desktop reserves the ends
            # for load/export); the message mirrors the desktop's wording.
            pct = 20 + int((index + 1) / self._total * 60)
            self._progress_cb(pct, f"Executing cell {index + 1}/{self._total}...")
        if self._on_cell:
            self._on_cell(index + 1, self._total)
        return super().preprocess_cell(cell, resources, index)


#: Excluded from the data fingerprint: the cache directory is written *inside* the node
#: it caches, so counting it would change the key on every render and the cache would
#: never hit once -- the exact opposite of the bug being fixed.
_FINGERPRINT_SKIP_DIRS = frozenset({".cache"})


def _data_fingerprint(data_dir: str) -> str:
    """A hash of what a notebook can read under *data_dir*: relative path + size.

    Deliberately **not** mtime, and deliberately not file contents.

    Not mtime, because the cluster service re-fetches a campaign on every request and
    that bumps every file's mtime -- keying on it would make the cache miss every time
    there, which is precisely why the data was left out of the key to begin with.

    Not contents, because a node can hold gigabytes of rosbag and this runs before every
    render. Path + size catches what actually happens to a results node: postprocessing
    adds files (poses.csv, data.db) and rewrites others. It cannot see an edit that keeps
    a file's size identical -- accepted, since results are written once by a pipeline
    rather than edited in place, and the alternative costs a full read of the campaign.
    """
    parts = []
    for root, dirs, files in os.walk(data_dir, topdown=True):
        dirs[:] = sorted(d for d in dirs if d not in _FINGERPRINT_SKIP_DIRS)
        for name in sorted(files):
            path = os.path.join(root, name)
            try:
                parts.append(f"{os.path.relpath(path, data_dir)}:{os.stat(path).st_size}")
            except OSError:
                # Vanished between listing and stat (a concurrent postprocess). Skipping
                # it only risks an extra render, never a stale one.
                continue
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def render_notebook_html(
    notebook_path: str,
    data_dir: str,
    *,
    inject: dict | None = None,
    theme: str = 'light',
    timeout: int = DEFAULT_TIMEOUT,
    progress_cb=None,
    on_cell=None,
    is_cancelled=None,
) -> str:
    """Execute *notebook_path* against *data_dir* and return the exported HTML.

    Args:
        notebook_path: The ``.ipynb`` to run (its ``DATA_DIR`` line is replaced).
        data_dir: The selected node's directory, injected as ``DATA_DIR``.
        inject: Extra ``NAME -> value`` assignments to substitute (e.g. ``BATCH``).
        theme: ``'light'`` or ``'dark'`` — drives the export theme + scrollbar CSS.
        timeout: Per-cell execution timeout in seconds.
        progress_cb: Optional ``callback(percent, message)`` for per-cell progress.
        on_cell: Optional ``callback(done, total)`` for per-cell progress as raw counts,
            for a caller that renders its own bar rather than a status line.
        is_cancelled: Optional ``callback() -> bool``; when it returns True mid-run,
            execution aborts with a ``RuntimeError``.

    Note that neither progress callback fires on a cache hit — there is nothing to execute,
    so the call returns before the executor is built.

    Returns:
        The self-contained HTML string (inputs hidden, outputs/plots shown).

    Raises:
        FileNotFoundError: The notebook does not exist.
        ValueError: The notebook has no ``DATA_DIR`` line.
        Exception: Any nbconvert execution/export error propagates unchanged.
    """
    if not os.path.isfile(notebook_path):
        raise FileNotFoundError(f"Notebook not found: {notebook_path}")

    nb_bytes = _read_bytes(notebook_path)

    # Cache lives next to the node's data (FileCache2 stores under ``<data_dir>/.cache``),
    # so each node caches independently. The key is the notebook content + injected vars
    # (a content hash survives the cluster's re-fetch, which bumps every file's mtime)
    # **and a fingerprint of the data the notebook reads** -- without which the render is
    # cached against the notebook alone, so viewing a campaign before postprocessing
    # cached "no poses.csv" and kept serving it afterwards. Postprocessing is the normal
    # way a node gains the data these notebooks plot, so that was the common case.
    cache = FileCache2(data_dir, "notebook_", suffix=".html")
    key = (
        CacheKey()
        .add("nb_sha", hashlib.sha256(nb_bytes).hexdigest())
        .add("inject", inject or {})
        .add("theme", theme)
        .add("data", _data_fingerprint(data_dir))
    )
    cached = cache.get(key)
    if cached is not None:
        return cached

    notebook = nbformat.reads(nb_bytes.decode("utf-8"), as_version=4)
    _apply_injections(notebook, data_dir, inject)

    # NB: do *not* force ``MPLBACKEND=Agg`` — ipykernel already uses the inline backend,
    # which captures ``plt.show()`` figures into the cell output as PNGs. Forcing Agg
    # overrides that, so figures are silently dropped ("FigureCanvasAgg is non-interactive").
    executor = _ProgressExecutePreprocessor(
        timeout=timeout, kernel_name="python3",
        progress_cb=progress_cb, on_cell=on_cell, is_cancelled=is_cancelled)
    executor.preprocess(notebook, {"metadata": {"path": data_dir}})

    exporter = HTMLExporter()
    exporter.template_name = "lab"
    exporter.theme = theme
    exporter.exclude_input = True
    exporter.exclude_input_prompt = True
    exporter.exclude_output_prompt = False
    (body, _) = exporter.from_notebook_node(notebook)
    body = inject_css_into_html_head(body, scrollbar_css(theme))

    cache.set(key, body)
    return body


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()
