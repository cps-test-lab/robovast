# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The campaign archive's layout ladder: the stamp, the steps, and their order.

What an import makes of a stamp is tested in ``tests/service/test_ingest.py``; the streams
that write it in ``tests/execution/test_campaign_archive.py``. This file holds the ladder to
its own contract, the way ``test_config_migrations.py`` holds the ``.vast`` ladder.
"""

import ast
import json
from pathlib import Path

import pytest

from robovast.common.migrations import archive
from robovast.common.migrations.archive import (ARCHIVE_LAYOUT, ARCHIVE_STAMP,
                                                BASELINE_ARCHIVE_LAYOUT, ArchiveLayoutError,
                                                ArchiveTooNew, read_layout, upgrade_archive)
from robovast.common.migrations.registry import surfaces

_STEPS = Path(archive.__file__).parent


def _tree(tmp_path) -> Path:
    """A small campaign tree with a record under ``_execution/``."""
    root = tmp_path / "camp-2026-01-01-000000"
    (root / "_execution").mkdir(parents=True)
    (root / "_execution" / "outcome.json").write_text('{"phase": "finished"}\n')
    (root / "_config").mkdir()
    (root / "_config" / "c.vast").write_text("version: 6\n")
    return root


def _snapshot(root: Path) -> dict:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*"))
            if p.is_file() and str(p.relative_to(root)) != ARCHIVE_STAMP}


def test_v0_to_v1_changes_nothing_in_the_tree_and_the_ladder_stamps_it(tmp_path):
    root = _tree(tmp_path)
    before = _snapshot(root)
    archive.v0_to_v1.migrate(root)
    assert _snapshot(root) == before
    found, applied = upgrade_archive(root)
    assert (found, applied[0]) == (0, "0_to_1")
    assert _snapshot(root) == before
    assert read_layout(root)[0] == ARCHIVE_LAYOUT


#: One test per step, named by the step. A step with no entry here fails
#: :func:`test_every_step_is_tested`, which is what makes the scaffold's placeholder fail.
_STEP_TESTS = {"0_to_1": test_v0_to_v1_changes_nothing_in_the_tree_and_the_ladder_stamps_it}


def test_every_step_is_tested():
    steps = [f"{v}_to_{v + 1}" for v in range(BASELINE_ARCHIVE_LAYOUT, ARCHIVE_LAYOUT)]
    assert sorted(_STEP_TESTS) == sorted(steps)


def test_no_step_reads_a_current_model():
    """A step that reads today's model changes meaning when that model changes."""
    forbidden = ("robovast.client.status", "robovast.common.config", "robovast.common.store",
                 "robovast.execution.control_server")
    for path in sorted(_STEPS.glob("v*_to_v*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                     else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            for name in names:
                assert not name.startswith(forbidden), f"{path.name} imports {name}"


def test_a_tree_without_a_stamp_is_the_baseline_layout(tmp_path):
    assert read_layout(_tree(tmp_path)) == (BASELINE_ARCHIVE_LAYOUT, {})


def test_the_current_layout_runs_no_step_and_leaves_the_stamp(tmp_path):
    root = _tree(tmp_path)
    (root / ARCHIVE_STAMP).write_text(json.dumps({"layout": ARCHIVE_LAYOUT}))
    assert upgrade_archive(root) == (ARCHIVE_LAYOUT, [])
    assert json.loads((root / ARCHIVE_STAMP).read_text()) == {"layout": ARCHIVE_LAYOUT}


def test_a_newer_layout_is_refused_naming_both(tmp_path):
    root = _tree(tmp_path)
    (root / ARCHIVE_STAMP).write_text(json.dumps({"layout": ARCHIVE_LAYOUT + 1}))
    with pytest.raises(ArchiveTooNew, match=f"layout {ARCHIVE_LAYOUT + 1}.*up to {ARCHIVE_LAYOUT}"):
        upgrade_archive(root)


@pytest.mark.parametrize("stamp", ['{"layout": -1}', '{"layout": true}', "{}", "[1]", "not json"])
def test_a_stamp_that_states_no_layout_is_refused(tmp_path, stamp):
    root = _tree(tmp_path)
    (root / ARCHIVE_STAMP).write_text(stamp)
    with pytest.raises(ArchiveLayoutError):
        read_layout(root)


def test_the_ladder_runs_each_step_in_order_and_restamps(tmp_path, monkeypatch):
    """The machinery, with a second step that rewrites a record: a layout-0 tree walks both
    steps in order, the record is rewritten, and the stamp names where it came from."""
    def rename_phase(root):
        path = root / "_execution" / "outcome.json"
        record = json.loads(path.read_text())
        record["state"] = record.pop("phase")
        path.write_text(json.dumps(record))

    order = []
    steps = [lambda root: order.append(0), lambda root: (order.append(1), rename_phase(root))]
    monkeypatch.setattr(archive, "_MIGRATIONS", steps)
    monkeypatch.setattr(archive, "ARCHIVE_LAYOUT", 2)
    root = _tree(tmp_path)
    (root / ARCHIVE_STAMP).write_text(json.dumps({"layout": 0, "robovast": "1.0.0"}))

    assert upgrade_archive(root) == (0, ["0_to_1", "1_to_2"])
    assert order == [0, 1]
    assert json.loads((root / "_execution" / "outcome.json").read_text()) == {"state": "finished"}
    assert json.loads((root / ARCHIVE_STAMP).read_text()) == {
        "layout": 2, "layout_from": 0, "robovast": "1.0.0"}


def test_a_failing_step_is_named(tmp_path, monkeypatch):
    def boom(_root):
        raise OSError("disk full")
    monkeypatch.setattr(archive, "_MIGRATIONS", [boom])
    with pytest.raises(ArchiveLayoutError, match="0_to_1 failed: disk full"):
        upgrade_archive(_tree(tmp_path))


def test_the_stamp_records_the_layout_and_the_other_surfaces():
    stamp = json.loads(archive.archive_stamp("camp-2026-01-01-000000"))
    assert stamp["layout"] == ARCHIVE_LAYOUT
    assert {"robovast", "config_version", "store_schema", "data_contract",
            "compat_version"} <= set(stamp)


def test_the_registry_lists_the_ladder():
    listed = {s["name"]: s for s in surfaces()}
    assert listed["campaign_archive"]["current"] == ARCHIVE_LAYOUT
    assert listed["campaign_archive"]["steps"] == ARCHIVE_LAYOUT - BASELINE_ARCHIVE_LAYOUT
