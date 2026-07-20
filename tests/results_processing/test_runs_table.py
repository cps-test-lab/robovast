# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Phase 0.4: the ``runs`` dimension table carries timing + sysinfo columns.

Enriching ``runs`` is what lets the per-run/per-config summary + search tools
collapse into SQL, so this asserts the new columns are built and populated from
each run's ``test.xml`` (timing) and ``sysinfo.yaml`` (host info).
"""

import json
import sqlite3

import pytest

from robovast.results_processing.postprocessing_plugins import _build_runs_table


def _write_run(run_dir, *, start_ts, duration, errors=0, failures=0):
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "test.xml").write_text(
        f'<testsuite errors="{errors}" failures="{failures}" tests="1">'
        f'<testcase time="{duration}">'
        f'<properties><property name="start_time" value="{start_ts}"/></properties>'
        f'</testcase></testsuite>',
        encoding="utf-8",
    )
    (run_dir / "sysinfo.yaml").write_text(
        "platform:\n  system: Linux\n"
        "cpu_name: Intel Xeon\n"
        "instance_type: n1-standard-4\n"
        "available_cpus: 4\n"
        "available_mem_gb: 16.0\n",
        encoding="utf-8",
    )


@pytest.fixture
def campaign_with_runs(tmp_path):
    # campaign.db with a `unit` row so params/objective join in.
    conn = sqlite3.connect(tmp_path / "campaign.db")
    conn.execute("CREATE TABLE unit (config_name TEXT, params_json TEXT, objective REAL)")
    conn.execute("INSERT INTO unit VALUES (?,?,?)",
                 ("cfg-a", json.dumps({"wind": 3.0}), 1.5))
    conn.commit()
    conn.close()

    cfg = tmp_path / "cfg-a"
    _write_run(cfg / "0", start_ts=1_700_000_000.0, duration=12.5)
    _write_run(cfg / "1", start_ts=1_700_000_100.0, duration=8.0, failures=1)
    return tmp_path, [cfg]


def test_runs_table_has_timing_and_sysinfo(campaign_with_runs):
    campaign_path, config_dirs = campaign_with_runs
    conn = sqlite3.connect(":memory:")
    _build_runs_table(conn, campaign_path, config_dirs)

    cols = {r[1] for r in conn.execute("PRAGMA table_info(runs)").fetchall()}
    for expected in ("start_time", "end_time", "instance_type", "cpu_name",
                     "available_cpus", "available_mem_gb"):
        assert expected in cols, f"missing column {expected}"

    row = conn.execute(
        "SELECT start_time, end_time, instance_type, cpu_name, available_cpus, "
        "available_mem_gb FROM runs WHERE config_name='cfg-a' AND run_id=0"
    ).fetchone()
    start_time, end_time, instance_type, cpu_name, cpus, mem = row

    assert start_time is not None and end_time is not None
    assert start_time < end_time  # end = start + duration
    assert instance_type == "n1-standard-4"
    assert cpu_name == "Intel Xeon"
    assert cpus == 4
    assert mem == pytest.approx(16.0)


def test_runs_table_tolerates_missing_sysinfo(tmp_path):
    """A run with no sysinfo.yaml must still produce a row (nulls, not a crash)."""
    conn0 = sqlite3.connect(tmp_path / "campaign.db")
    conn0.execute("CREATE TABLE unit (config_name TEXT, params_json TEXT, objective REAL)")
    conn0.execute("INSERT INTO unit VALUES ('cfg-x', '{}', NULL)")
    conn0.commit()
    conn0.close()

    cfg = tmp_path / "cfg-x"
    run = cfg / "0"
    run.mkdir(parents=True)
    (run / "test.xml").write_text(
        '<testsuite errors="0" failures="0" tests="1"><testcase time="1.0">'
        '<properties><property name="start_time" value="1700000000.0"/></properties>'
        '</testcase></testsuite>', encoding="utf-8")

    conn = sqlite3.connect(":memory:")
    _build_runs_table(conn, tmp_path, [cfg])
    row = conn.execute("SELECT instance_type, cpu_name FROM runs").fetchone()
    assert row == (None, None)
