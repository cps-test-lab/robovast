# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""SQL over campaign directories: tables built when named, answered by a locked connection."""

import json

import pytest
import yaml

from robovast_data import Engine, QueryError, Scope
from robovast_decode import build as decode_build

from .conftest import nav_campaign, write_store


def _manifest(campaign):
    return json.loads((campaign / ".cache" / "MANIFEST.json").read_text())


def _built(campaign, table):
    return sorted(_manifest(campaign)["tables"].get(table, {}).get("runs", {}))


def test_a_table_is_built_on_first_use_and_read_on_the_second(campaign, monkeypatch):
    engine = Engine([Scope(str(campaign))], workers=1)
    assert engine.arrow("SELECT count(*) AS n FROM poses").column("n")[0].as_py() == 3 * 244
    assert _built(campaign, "poses") == ["cfg-a/0", "cfg-a/1", "cfg-b/0"]

    def refuse(*_args, **_kwargs):
        raise AssertionError("a finished run's table is not looked at again")
    monkeypatch.setattr("robovast_data.engine.build", refuse)
    assert engine.arrow("SELECT count(*) AS n FROM poses").column("n")[0].as_py() == 3 * 244


def test_a_query_for_one_run_builds_that_run_only(campaign):
    engine = Engine([Scope(str(campaign))], workers=1)
    engine.arrow("SELECT * FROM poses WHERE config_name = 'cfg-a' AND run_id = 1")
    assert _built(campaign, "poses") == ["cfg-a/1"]


def test_a_query_naming_no_run_builds_every_run_in_scope_in_workers(campaign):
    seen = []
    engine = Engine([Scope(str(campaign))], workers=2,
                    progress=lambda done, total: seen.append((done, total)))
    engine.arrow("SELECT count(*) FROM rosbag2_collision")
    assert _built(campaign, "rosbag2_collision") == ["cfg-a/0", "cfg-a/1", "cfg-b/0"]
    assert seen[-1] == (3, 3)


def test_a_run_scope_sees_and_builds_its_run_only(campaign):
    engine = Engine([Scope(str(campaign), "cfg-b", 0)], workers=1)
    rows = engine.arrow("SELECT DISTINCT config_name, run_id FROM poses").to_pylist()
    assert rows == [{"config_name": "cfg-b", "run_id": 0}]
    assert _built(campaign, "poses") == ["cfg-b/0"]
    assert engine.arrow("SELECT config_name FROM runs").to_pylist() == [
        {"config_name": "cfg-b"}]
    assert engine.arrow("SELECT count(*) AS n FROM run_view").column("n")[0].as_py() == 1


def test_several_campaigns_are_one_query_with_their_ids(tmp_path):
    first = nav_campaign(tmp_path / "c1")
    second = nav_campaign(tmp_path / "c2")
    engine = Engine([Scope(str(first)), Scope(str(second))], workers=1)
    rows = engine.arrow("SELECT campaign_id, count(*) AS n FROM poses GROUP BY 1 ORDER BY 1")
    assert rows.to_pylist() == [{"campaign_id": "c1", "n": 244}, {"campaign_id": "c2", "n": 244}]
    ids = engine.arrow("SELECT DISTINCT campaign_id FROM campaign.run ORDER BY 1")
    assert ids.column(0).to_pylist() == ["c1", "c2"]


@pytest.mark.parametrize("sql", [
    "SELECT * FROM read_csv('/etc/passwd')",
    "SELECT * FROM read_text('/etc/hostname')",
])
def test_a_query_reads_nothing_outside_the_campaigns_tables(campaign, sql):
    with pytest.raises(QueryError, match="disabled by configuration|Permission"):
        Engine([Scope(str(campaign))], workers=1).arrow(sql)


def test_a_query_past_its_time_is_stopped(campaign):
    engine = Engine([Scope(str(campaign))], workers=1, timeout_s=0.2)
    with pytest.raises(QueryError, match="ran past"):
        engine.arrow("SELECT count(*) FROM range(1000000000) a, range(1000000) b")


def test_the_functions_older_queries_use(campaign):
    engine = Engine([Scope(str(campaign))], workers=1)
    row = engine.arrow("SELECT PERCENTILE(x, 95) AS p, PERCENTILE(x, 150) AS clamped, "
                       "MEDIAN(x) AS m, bool_or(REGEXP('^1', CAST(x AS VARCHAR))) AS r, "
                       "bool_or(REGEXP(NULL, 'x')) AS null_pattern "
                       "FROM (SELECT 1.0::DOUBLE AS x UNION ALL SELECT 3.0::DOUBLE)").to_pylist()[0]
    assert row["p"] == pytest.approx(2.9) and row["clamped"] == 3.0 and row["m"] == 2.0
    assert row["r"] is True and row["null_pattern"] is False


def test_a_missing_table_names_what_there_is(campaign):
    with pytest.raises(QueryError, match="Defined here: .*run_view"):
        Engine([Scope(str(campaign))], workers=1).arrow("SELECT * FROM nothing_like_it")


def test_a_table_that_failed_for_a_run_is_reported_with_the_answer(campaign):
    (campaign / "_execution").mkdir(exist_ok=True)
    (campaign / "_execution" / "tables.yaml").write_text(yaml.safe_dump({"groups": [
        {"bag_dir": "rosbag2", "plugins": [
            {"type": "tf_to_csv", "frames": "all", "require": ["nowhere"]}]}]}))
    engine = Engine([Scope(str(campaign), "cfg-a", 0)], workers=1)
    prepared = engine.prepare("SELECT * FROM poses")
    assert [(p.table, p.run) for p in prepared.problems] == [("poses", "cfg-a/0")]
    assert "nowhere" in prepared.problems[0].reason
    with engine.execute("SELECT * FROM poses") as (con, problems):
        assert con.fetchall() == [] and problems
    with pytest.raises(QueryError, match="nowhere"):
        engine.arrow('SELECT "position.x" FROM poses')


def test_run_view_keeps_a_unit_that_produced_no_run(tmp_path):
    root = nav_campaign(tmp_path / "c")
    import sqlite3
    db = sqlite3.connect(root / "campaign.db")
    db.execute("INSERT INTO unit (batch_id, paramset_id, config_name, params_json, status) "
               "VALUES (1, 'ps-9', NULL, '{}', 'composition_failed')")
    db.commit()
    db.close()
    rows = Engine([Scope(str(root))], workers=1).arrow(
        "SELECT config_name, run_id, status FROM run_view ORDER BY 1").to_pylist()
    assert {"config_name": "ps-9", "run_id": None, "status": "composition_failed"} in rows


def test_container_failure_view_keeps_a_failure_that_named_no_run(tmp_path):
    root = tmp_path / "c"
    nav_campaign(root)
    (root / "campaign.db").unlink()
    write_store(root, {"cfg": {"runs": {0: "passed"}}}, failures=(["cfg/0", "cfg/1"], []))
    rows = Engine([Scope(str(root))], workers=1).arrow(
        "SELECT run_key FROM container_failure_view ORDER BY run_key NULLS LAST").to_pylist()
    assert rows == [{"run_key": "cfg/0"}, {"run_key": "cfg/1"}, {"run_key": None}]


def test_config_view_has_json_trees_shape(tmp_path):
    root = tmp_path / "c"
    nav_campaign(root)
    (root / "campaign.db").unlink()
    write_store(root, {"cfg": {"runs": {0: "passed"}}}, config_json={
        "execution": {"containers": {"scenario": {"cpu": 2.5}}}, "on": True,
        "odd-key": [1, "a"]})
    rows = {r["fullkey"]: r for r in Engine([Scope(str(root))], workers=1).arrow(
        "SELECT * FROM config_view").to_pylist()}
    assert rows["$.execution.containers.scenario.cpu"]["type"] == "real"
    assert rows["$.execution.containers.scenario.cpu"]["value"] == "2.5"
    assert rows["$.on"]["type"] == "true" and rows["$.on"]["value"] == "1"
    assert rows['$."odd-key"']["type"] == "array" and rows['$."odd-key"']["value"] is None
    assert rows['$."odd-key"[1]']["value"] == "a"
    assert rows['$."odd-key"[1]']["parent"] == '$."odd-key"'


def test_run_validity_view_reads_system_usage(campaign):
    for job in (campaign / "_jobs").iterdir():
        (job / "system_usage_main.csv").write_text(
            "timestamp,nr_periods,nr_throttled,throttled_usec\n"
            "0,0,0,0\n10,1000,100,500\n")
    rows = Engine([Scope(str(campaign))], workers=1).arrow(
        "SELECT config_name, run_id, throttle_ratio, quota_bound, stalled_full_usec "
        "FROM run_validity_view ORDER BY 1, 2").to_pylist()
    assert len(rows) == 3
    assert rows[0]["throttle_ratio"] == 0.1 and rows[0]["quota_bound"] == 1
    assert rows[0]["stalled_full_usec"] is None, "not measured, never zero"


def test_pose_track_view_covers_every_pose_table(campaign):
    (campaign / "cfg-a" / "0" / "sim_poses.csv").write_text(
        "timestamp,frame,position.x,position.y\n0,robot,0,0\n1,robot,3,4\n")
    rows = Engine([Scope(str(campaign))], workers=1).arrow(
        "SELECT source, frame, length_m FROM pose_track_view WHERE source = 'sim_poses'")
    assert rows.to_pylist() == [{"source": "sim_poses", "frame": "robot", "length_m": 5.0}]
    assert _built(campaign, "poses") == ["cfg-a/0", "cfg-a/1", "cfg-b/0"]


def test_the_catalog_says_what_can_be_built_and_what_is(campaign):
    engine = Engine([Scope(str(campaign))], workers=1)
    engine.arrow("SELECT * FROM poses WHERE config_name = 'cfg-b'")
    catalog = engine.catalog()
    assert catalog["poses"]["runs"] == 3 and catalog["poses"]["built"] == 1
    assert ["position.x", "double"] in catalog["poses"]["columns"]
    assert catalog["rosbag2_collision"]["built"] == 0
    assert catalog["rosbag2_collision"]["columns"] is None
    assert catalog["runs"]["kind"] == "table" and catalog["run_view"]["kind"] == "view"
    assert catalog["campaign.unit"]["kind"] == "record"
    assert catalog["pose_track_view"]["kind"] == "view"


def test_a_directory_that_is_not_a_campaign_is_refused(tmp_path):
    with pytest.raises(FileNotFoundError):
        Engine([Scope(str(tmp_path / "nope"))])


def test_the_decoder_used_is_the_one_the_manifest_names(campaign):
    Engine([Scope(str(campaign))], workers=1).arrow("SELECT count(*) FROM poses")
    entries = _manifest(campaign)["tables"]["poses"]["runs"].values()
    assert {e["decoder"] for e in entries} == {decode_build.__version__}
