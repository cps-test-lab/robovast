# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``postprocessing_steps``: which step produced which table, with what, beside the data.

The table is documented to agents by ``data_query._TABLE_DESCRIPTIONS`` and pointed at by
the MCP results prompts. It is a campaign table: written once by the campaign-end pass from
the entries postprocessing collected, never from the provenance record, which is written
after it.
"""

import json

from robovast.results_processing.campaign_tables import write_postprocessing_steps
from robovast.results_processing.data_query import describe_data_db, query_data_db
from robovast_decode import __version__ as DECODER_VERSION

from .conftest import write_campaign_db

#: One step per kind the resolution has to distinguish: an output that became a table, one
#: whose stem is sanitised on the way (``nav-metrics.csv`` -> ``nav_metrics``), and one that
#: is not a data file at all.
ENTRIES = [
    {"plugin": "pose_extract", "output": "cfg-a/0/tracked.csv",
     "sources": ["rosbag2"], "params": {"topic": "/amcl_pose"}},
    {"plugin": "nav_metrics", "output": "cfg-a/0/nav-metrics.csv",
     "sources": ["cfg-a/0/tracked.csv"], "params": {}},
    {"plugin": "plot_paths", "output": "plots/paths.png", "sources": [], "params": {}},
]


def _campaign(tmp_path, name="camp-a"):
    root = tmp_path / name
    write_campaign_db(root, name)
    for config in ("cfg-a", "cfg-b"):
        for run_id in (0, 1):
            run_dir = root / config / str(run_id)
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "tracked.csv").write_text(
                "timestamp,stamp,position.x,orientation.yaw\n0.5,0.4,1.0,0.1\n1.5,1.4,2.0,0.2\n")
            (run_dir / "nav-metrics.csv").write_text("duration_s\n12.5\n")
    return root


def _steps(root):
    return query_data_db(
        root, "SELECT step_idx, plugin, output, table_name, sources_json, params_json "
              "FROM postprocessing_steps ORDER BY step_idx")["rows"]


def test_one_row_per_step_with_the_table_its_output_became(tmp_path):
    """The provenance edge, joinable: which plugin produced which table, with what params."""
    root = _campaign(tmp_path)
    assert write_postprocessing_steps(str(root), ENTRIES) == 3

    rows = _steps(root)
    assert [(r["step_idx"], r["plugin"], r["table_name"]) for r in rows] == [
        (0, "pose_extract", "tracked"),
        (1, "nav_metrics", "nav_metrics"),
        # Not a data file that became a table: NULL is a fact about the step.
        (2, "plot_paths", None),
    ]
    assert json.loads(rows[0]["sources_json"]) == ["rosbag2"]
    assert json.loads(rows[0]["params_json"]) == {"topic": "/amcl_pose"}


def test_a_decoded_table_is_a_step_with_the_decoder_version(tmp_path):
    """A rebuild with a newer decoder is a recorded event, not a silent change."""
    root = _campaign(tmp_path)
    query_data_db(root, "SELECT count(*) FROM tracked")
    write_postprocessing_steps(str(root), ENTRIES)

    decoded = [r for r in _steps(root) if r["plugin"] == "robovast-decode"]
    assert "tracked" in {r["table_name"] for r in decoded}
    assert all(json.loads(r["params_json"]) == {"decoder": DECODER_VERSION} for r in decoded)
    assert "postprocessing_steps" not in {r["table_name"] for r in decoded}


def test_the_table_exists_even_when_no_postprocessing_ran(tmp_path):
    """Absent says "never postprocessed"; empty says it ran and had no steps."""
    root = _campaign(tmp_path)
    assert write_postprocessing_steps(str(root), []) == 0
    assert _steps(root) == []


def test_rewriting_reproduces_identical_rows(tmp_path):
    root = _campaign(tmp_path)
    write_postprocessing_steps(str(root), ENTRIES)
    before = _steps(root)
    write_postprocessing_steps(str(root), ENTRIES)
    assert _steps(root) == before


def test_the_steps_need_no_record_on_disk(tmp_path):
    """The provenance record is written last, so that its presence means postprocessing
    finished; the steps are written before it and must come from the entries passed."""
    root = _campaign(tmp_path)
    write_postprocessing_steps(str(root), ENTRIES)
    assert not (root / "_transient" / "postprocessing.yaml").exists()
    assert len(_steps(root)) == 3


def test_the_pose_notes_are_served_beside_the_column(tmp_path):
    """What makes the notes a contract: ``describe`` shows them where a join is chosen."""
    root = _campaign(tmp_path)
    query_data_db(root, "SELECT count(*) FROM tracked")
    tracked = next(t for t in describe_data_db(root)["tables"] if t["table"] == "tracked")
    assert "ARRIVAL time" in tracked["column_notes"]["timestamp"]
