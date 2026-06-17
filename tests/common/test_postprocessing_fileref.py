# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Postprocessing plugins load identically from entry points or local files.

Both ``results_processing.postprocessing`` and ``search.postprocessing`` resolve
a command name as either an entry-point name or a ``./path.py:Class`` local file
reference, via the shared resolver/runner.
"""

import textwrap

import pytest

from robovast.results_processing.postprocessing import (
    resolve_postprocessing_plugin, run_postprocessing_commands)

PLUGIN_SRC = textwrap.dedent("""\
    from pathlib import Path
    from robovast.results_processing.postprocessing_plugins import BasePostprocessingPlugin

    class MarkRuns(BasePostprocessingPlugin):
        def __call__(self, results_dir, config_dir, tag="touched", **kwargs):
            n = 0
            for test in Path(results_dir).rglob("test.xml"):
                (test.parent / "metrics.csv").write_text(tag + "\\n")
                n += 1
            return True, f"marked {n} run(s)"
""")


def _write_plugin(d):
    f = d / "myplugin.py"
    f.write_text(PLUGIN_SRC)
    return f


def test_resolve_file_ref_plugin(tmp_path):
    _write_plugin(tmp_path)
    plugin = resolve_postprocessing_plugin("myplugin.py:MarkRuns", str(tmp_path))
    assert plugin.__class__.__name__ == "MarkRuns"


def test_resolve_entry_point_name():
    # A built-in entry-point name resolves to its instance.
    plugin = resolve_postprocessing_plugin("command", config_dir="")
    assert plugin.__class__.__name__ == "Command"


def test_resolve_unknown_raises(tmp_path):
    with pytest.raises(KeyError):
        resolve_postprocessing_plugin("does_not_exist", str(tmp_path))


def test_run_commands_with_file_ref(tmp_path):
    _write_plugin(tmp_path)
    campaign = tmp_path / "campaign-x"
    for run in range(2):
        rd = campaign / "ca" / str(run)
        rd.mkdir(parents=True)
        (rd / "test.xml").write_text("<testsuite/>")

    ok, _ = run_postprocessing_commands(
        ["myplugin.py:MarkRuns", {"myplugin.py:MarkRuns": {"tag": "v2"}}],
        results_dir=str(campaign), config_dir=str(tmp_path), output=lambda *_: None)
    assert ok
    # Both runs got a metrics.csv, last command's tag wins.
    for run in range(2):
        p = campaign / "ca" / str(run) / "metrics.csv"
        assert p.exists() and p.read_text().strip() == "v2"
