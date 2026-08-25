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
               available_mem="134603354112", node_name="worker-a"):
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
        f"node_name: {node_name}\n"
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
    for expected in ("start_time", "end_time", "instance_type", "node_name", "cpu_name",
                     "available_cpus", "available_mem_bytes"):
        assert expected in cols, f"missing column {expected}"

    row = conn.execute(
        "SELECT start_time, end_time, instance_type, node_name, cpu_name, available_cpus, "
        "available_mem_bytes FROM runs WHERE config_name='cfg-a' AND run_id=0"
    ).fetchone()
    start_time, end_time, instance_type, node_name, cpu_name, cpus, mem = row

    assert start_time is not None and end_time is not None
    assert start_time < end_time  # end = start + duration
    assert instance_type == "n1-standard-4"
    # The machine, not its kind: on a bare-metal cluster instance_type is `uname -m` and
    # is identical on every node, so only this separates a slow machine from a fast one.
    assert node_name == "worker-a"
    assert cpu_name == "Intel Xeon"
    assert cpus == 4
    assert mem == 134603354112, "the recorded byte count, not NULL and not rescaled"


def test_a_run_that_recorded_no_node_reports_null_rather_than_an_empty_name(tmp_path):
    """``NODE_NAME`` is empty on the local lane and on any cluster run recorded before the
    pod carried it. NULL keeps those out of a GROUP BY node_name; "" would collect them
    all under one machine that does not exist."""
    conn0 = sqlite3.connect(tmp_path / "campaign.db")
    conn0.execute("CREATE TABLE unit (config_name TEXT, params_json TEXT, objective REAL)")
    conn0.execute("INSERT INTO unit VALUES ('cfg-a', '{}', NULL)")
    conn0.commit()
    conn0.close()

    cfg = tmp_path / "cfg-a"
    _write_run(cfg / "0", start_ts=1_700_000_000.0, duration=1.0, node_name="")

    conn = sqlite3.connect(":memory:")
    _build_runs_table(conn, tmp_path, [cfg])
    assert conn.execute("SELECT node_name FROM runs").fetchone()[0] is None


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


def test_composition_failed_units_reach_the_runs_table(tmp_path):
    """A search draw that never composed has no directory, so the config-dir walk
    cannot see it — yet it is exactly the record that says the search space is partly
    unrealizable. It must arrive as a run-less row carrying its parameters, or a
    campaign that failed to build half its draws reads as one that proposed fewer.
    """
    conn0 = sqlite3.connect(tmp_path / "campaign.db")
    conn0.execute("CREATE TABLE unit (config_name TEXT, params_json TEXT, "
                  "objective REAL, status TEXT, paramset_id TEXT)")
    conn0.execute("INSERT INTO unit VALUES (?,?,?,?,?)",
                  ("cfg-a", json.dumps({"wind": 3.0}), 1.5, "evaluated", "ps-1"))
    # No config_name and no result dir — all it has is its paramset_id and its params.
    conn0.execute("INSERT INTO unit VALUES (?,?,?,?,?)",
                  ("", json.dumps({"wind": 99.0}), None, "composition_failed", "ps-2"))
    conn0.commit()
    conn0.close()

    cfg = tmp_path / "cfg-a"
    _write_run(cfg / "0", start_ts=1_700_000_000.0, duration=1.0)

    conn = sqlite3.connect(":memory:")
    _build_runs_table(conn, tmp_path, [cfg])

    row = conn.execute(
        "SELECT config_name, run_id, status, param_wind FROM runs "
        "WHERE status='composition_failed'").fetchone()
    assert row is not None, "the failed draw vanished from the runs table"
    config_name, run_id, _status, wind = row
    assert config_name == "ps-2"      # falls back to the paramset id
    assert run_id is None             # there is no run to number
    assert wind == 99.0               # the parameters are the point of the row

    # The real run is unaffected, and run statistics can still exclude the draw.
    assert conn.execute(
        "SELECT COUNT(*) FROM runs WHERE run_id IS NOT NULL").fetchone()[0] == 1


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
    row = conn.execute("SELECT instance_type, node_name, cpu_name FROM runs").fetchone()
    assert row == (None, None, None)


# -- the run's shared-memory pool ---------------------------------------------------------


def _campaign_with_one_run(tmp_path, resource_rows=None):
    """A campaign of one run, optionally with the per-run resource table postprocessing
    writes. *resource_rows* are ``(shm_used, shm_total)`` cells, written verbatim."""
    conn0 = sqlite3.connect(tmp_path / "campaign.db")
    conn0.execute("CREATE TABLE unit (config_name TEXT, params_json TEXT, objective REAL)")
    conn0.execute("INSERT INTO unit VALUES ('cfg-a', '{}', NULL)")
    conn0.commit()
    conn0.close()

    cfg = tmp_path / "cfg-a"
    _write_run(cfg / "0", start_ts=1_700_000_000.0, duration=5.0)
    if resource_rows is not None:
        (cfg / "0" / "resource_usage.csv").write_text(
            "timestamp,wall_ts,in_window,container,name,cpu_percent,memory_rss_bytes,"
            "num_pids,shm_used_bytes,shm_total_bytes\n"
            + "".join(f",{100 + i}.0,1,robovast,gzserver,10.00,1024,1,{used},{total}\n"
                      for i, (used, total) in enumerate(resource_rows)),
            encoding="utf-8")
    conn = sqlite3.connect(":memory:")
    _build_runs_table(conn, tmp_path, [cfg])
    return conn


def test_runs_carries_the_shm_high_water_mark(tmp_path):
    """The figure ``execution.shm_size`` has to cover, beside ``available_mem_bytes`` --
    which is process memory only, while the pool is a tmpfs charged to the pod on top of it.
    A container that overruns it dies of SIGBUS (exit 135) rather than a clean OOM, so this
    is the number that explains such a death."""
    conn = _campaign_with_one_run(tmp_path, [(5_000_000, 1_073_741_824),
                                             (900_000_000, 1_073_741_824),
                                             (7_000_000, 1_073_741_824)])
    assert conn.execute(
        "SELECT shm_peak_bytes, shm_limit_bytes FROM runs").fetchone() == (
            900_000_000, 1_073_741_824)


def test_an_unmeasured_pool_is_null_rather_than_zero(tmp_path):
    """Two runs reach this: one recorded before the monitor sampled the pool (no columns at
    all) and one on a runtime without /dev/shm (empty cells). Neither used none of it, and a
    0 here would report both as a campaign that measured an empty pool."""
    assert _campaign_with_one_run(tmp_path).execute(
        "SELECT shm_peak_bytes, shm_limit_bytes FROM runs").fetchone() == (None, None)

    other = tmp_path / "other"
    other.mkdir()
    assert _campaign_with_one_run(other, [("", "")]).execute(
        "SELECT shm_peak_bytes, shm_limit_bytes FROM runs").fetchone() == (None, None)
