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


def _write_run(run_dir, *, start_ts, duration, errors=0, failures=0,
               available_mem="134603354112"):
    """A run dir with the artifacts the runner actually writes.

    ``available_mem`` is spelled exactly as ``collect_sysinfo.py`` writes it — that key,
    and a Kubernetes-style quantity for its value. An earlier version of this fixture
    invented ``available_mem_gb``, which no producer emits, so it agreed with an ingest
    that read the same wrong key and the column was NULL in every real campaign while
    this test passed.
    """
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
        f"available_mem: {available_mem}\n",
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
                     "available_cpus", "available_mem_bytes"):
        assert expected in cols, f"missing column {expected}"

    row = conn.execute(
        "SELECT start_time, end_time, instance_type, cpu_name, available_cpus, "
        "available_mem_bytes FROM runs WHERE config_name='cfg-a' AND run_id=0"
    ).fetchone()
    start_time, end_time, instance_type, cpu_name, cpus, mem = row

    assert start_time is not None and end_time is not None
    assert start_time < end_time  # end = start + duration
    assert instance_type == "n1-standard-4"
    assert cpu_name == "Intel Xeon"
    assert cpus == 4
    assert mem == 134603354112, "the recorded byte count, not NULL and not rescaled"


def test_runs_table_normalizes_a_suffixed_memory_quantity(tmp_path):
    """A .vast that sets ``resources.memory`` puts a suffixed string in sysinfo.

    Stored raw, that would make the column numeric for cluster runs and text for these,
    where AVG() reads the text rows as 0 and returns a plausible wrong number.
    """
    conn = sqlite3.connect(tmp_path / "campaign.db")
    conn.execute("CREATE TABLE unit (config_name TEXT, params_json TEXT, objective REAL)")
    conn.execute("INSERT INTO unit VALUES (?,?,?)", ("cfg-a", "{}", None))
    conn.commit()
    conn.close()

    cfg = tmp_path / "cfg-a"
    _write_run(cfg / "0", start_ts=1_700_000_000.0, duration=1.0, available_mem="16Gi")

    conn = sqlite3.connect(":memory:")
    _build_runs_table(conn, tmp_path, [cfg])
    mem, typ = conn.execute(
        "SELECT available_mem_bytes, typeof(available_mem_bytes) FROM runs").fetchone()
    assert mem == 16 * 1024 ** 3
    assert typ == "integer"


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
