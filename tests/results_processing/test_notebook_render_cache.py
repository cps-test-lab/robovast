# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The evaluation-notebook render cache is keyed on the DATA as well as the notebook.

It was keyed on the notebook content, the injected vars and the theme -- everything
except the thing the notebook actually reads. So the normal sequence of using the
Results explorer produced a wrong answer that never healed:

    view a campaign          -> renders "[no data] no poses.csv", caches it
    run postprocessing       -> poses.csv, data.db appear
    view the campaign again  -> served from cache, still "[no data] no poses.csv"

and the only ways out were editing the notebook or deleting <node>/.cache by hand.

The fingerprint has two properties that pull against each other, and both are pinned
here: it must change when postprocessing adds results, and it must NOT change merely
because the cluster service re-downloaded the campaign (which rewrites every mtime).
That is why it is path + size, not mtime and not content.
"""

import json
import os

from robovast.results_processing.notebook_render import _data_fingerprint, render_notebook_html


def _touch(path, text="x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


# -- the fingerprint ---------------------------------------------------------


def test_new_results_change_the_fingerprint(tmp_path):
    """The bug: postprocessing adds poses.csv and the render must be redone."""
    _touch(str(tmp_path / "cfg" / "0" / "test.xml"))
    before = _data_fingerprint(str(tmp_path))
    _touch(str(tmp_path / "cfg" / "0" / "poses.csv"), "frame,timestamp\n")
    assert _data_fingerprint(str(tmp_path)) != before


def test_rewriting_a_file_larger_changes_the_fingerprint(tmp_path):
    _touch(str(tmp_path / "out.csv"), "a\n")
    before = _data_fingerprint(str(tmp_path))
    _touch(str(tmp_path / "out.csv"), "a\nb\nc\n")
    assert _data_fingerprint(str(tmp_path)) != before


def test_a_re_download_does_not_change_the_fingerprint(tmp_path):
    """The property that kept the data out of the key in the first place.

    The cluster service re-fetches a campaign on every request, bumping every mtime. A
    key that noticed mtime would miss on every single cluster request -- turning a
    stale-cache bug into a no-cache bug, and every view into a full notebook execution.
    """
    target = tmp_path / "cfg" / "0" / "test.xml"
    _touch(str(target))
    before = _data_fingerprint(str(tmp_path))
    os.utime(target, (10_000_000, 10_000_000))  # same bytes, new mtime
    assert _data_fingerprint(str(tmp_path)) == before


def test_the_cache_dir_is_not_part_of_the_fingerprint(tmp_path):
    """FileCache2 writes inside the node it caches.

    Counting it would change the key every time a render is stored, so the next lookup
    would miss -- the cache would never hit once. That is a worse bug than the one
    being fixed, and an easy one to introduce here.
    """
    _touch(str(tmp_path / "test.xml"))
    before = _data_fingerprint(str(tmp_path))
    _touch(str(tmp_path / ".cache" / "notebook_abc.html"), "<html/>")
    assert _data_fingerprint(str(tmp_path)) == before


def test_the_fingerprint_is_order_independent(tmp_path):
    """Two nodes with the same files must agree regardless of creation order, or the
    cache would miss for reasons no one can see."""
    a, b = tmp_path / "a", tmp_path / "b"
    for name in ("one.csv", "two.csv"):
        _touch(str(a / name), name)
    for name in ("two.csv", "one.csv"):
        _touch(str(b / name), name)
    assert _data_fingerprint(str(a)) == _data_fingerprint(str(b))


# -- end to end --------------------------------------------------------------


NOTEBOOK = {
    "cells": [{
        "cell_type": "code", "id": "c0", "execution_count": None,
        "metadata": {}, "outputs": [],
        "source": ["DATA_DIR = ''\n",
                   "import os\n",
                   "names = sorted(f for _r, _d, fs in os.walk(DATA_DIR) for f in fs\n"
                   "               if not _r.endswith('.cache'))\n",
                   "print('FILES:', ','.join(names))\n"],
    }],
    "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python",
                                "name": "python3"},
                 "language_info": {"name": "python"}},
    "nbformat": 4, "nbformat_minor": 5,
}


def test_a_render_is_redone_once_the_node_gains_results(tmp_path):
    """The reported sequence, end to end: view, postprocess, view again."""
    nb = tmp_path / "analysis.ipynb"
    nb.write_text(json.dumps(NOTEBOOK), encoding="utf-8")
    node = tmp_path / "node"
    _touch(str(node / "test.xml"))

    first = render_notebook_html(notebook_path=str(nb), data_dir=str(node))
    assert "test.xml" in first and "poses.csv" not in first

    # A second view with nothing changed must be served from the cache -- byte-identical,
    # and no re-execution. Without this the fix would just be "never cache".
    assert render_notebook_html(notebook_path=str(nb), data_dir=str(node)) == first

    _touch(str(node / "poses.csv"), "frame,timestamp\n")  # postprocessing ran
    after = render_notebook_html(notebook_path=str(nb), data_dir=str(node))
    assert "poses.csv" in after, "the cached pre-postprocessing render was served again"
