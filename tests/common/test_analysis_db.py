# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Reading a campaign's results from data.db, scoped to a notebook's DATA_DIR."""

import sqlite3

import pytest

from robovast.common.analysis import (DATA_DB_SCHEMA_VERSION, CampaignDataError, attach_params,
                                      campaign_root, config_file, get_behavior_info, list_tables,
                                      read_runs, read_table, run_scope, table_info)

BEHAVIOURS = [
    # (config_name, run_id, timestamp, behavior_name, behavior_id, status_name)
    ("ca", 0, 0.0, "nav", "b1", "INVALID"),
    ("ca", 0, 1.0, "nav", "b1", "RUNNING"),
    ("ca", 0, 4.0, "nav", "b1", "SUCCESS"),
    ("ca", 1, 1.0, "nav", "b1", "RUNNING"),
    ("ca", 1, 9.0, "nav", "b1", "SUCCESS"),
    ("cb", 0, 2.0, "nav", "b1", "RUNNING"),
    ("cb", 0, 5.0, "nav", "b1", "FAILURE"),
]


def _campaign(tmp_path, *, with_data_db=True, version=DATA_DB_SCHEMA_VERSION,
              behaviours=True):
    """A campaign directory shaped like a real one: configs, runs, and the two databases."""
    root = tmp_path / "camp-2026-08-10-07150919"
    for config, run in (("ca", 0), ("ca", 1), ("cb", 0)):
        (root / config / str(run)).mkdir(parents=True)
    (root / "campaign.db").touch()
    if not with_data_db:
        return root

    (root / "_execution").mkdir()
    conn = sqlite3.connect(root / "_execution" / "data.db")
    conn.execute(f"PRAGMA user_version = {version}")
    conn.execute("CREATE TABLE _table_name_map (display_name TEXT, sql_name TEXT)")
    conn.execute("CREATE TABLE runs (config_name TEXT, run_id INTEGER, status TEXT, "
                 "param_speed REAL)")
    conn.executemany("INSERT INTO runs VALUES (?, ?, ?, ?)",
                     [("ca", 0, "passed", 1.0), ("ca", 1, "passed", 1.0),
                      ("cb", 0, "failed", 2.0)])
    if behaviours:
        conn.execute("CREATE TABLE behaviors (config_name TEXT, run_id INTEGER, "
                     "timestamp REAL, behavior_name TEXT, behavior_id TEXT, status_name TEXT)")
        conn.executemany("INSERT INTO behaviors VALUES (?, ?, ?, ?, ?, ?)", BEHAVIOURS)
    conn.commit()
    conn.close()
    return root


def test_campaign_root_is_found_from_every_node(tmp_path):
    root = _campaign(tmp_path)
    assert campaign_root(root) == root
    assert campaign_root(root / "ca") == root
    assert campaign_root(root / "ca" / "0") == root


def test_campaign_root_does_not_depend_on_the_directory_name(tmp_path):
    """A results directory gets renamed and copied; the campaign is still the campaign."""
    root = _campaign(tmp_path)
    renamed = tmp_path / "not-a-campaign-shaped-name"
    root.rename(renamed)
    assert campaign_root(renamed / "ca" / "0") == renamed


def test_run_scope_reads_the_three_levels(tmp_path):
    root = _campaign(tmp_path)
    assert run_scope(root) == ("campaign", None, None)
    assert run_scope(root / "ca") == ("config", "ca", None)
    assert run_scope(root / "ca" / "0") == ("run", "ca", 0)


def test_reading_narrows_to_the_selected_node(tmp_path):
    root = _campaign(tmp_path)
    assert len(read_table(root, "behaviors")) == len(BEHAVIOURS)
    assert len(read_table(root / "ca", "behaviors")) == 5
    assert len(read_table(root / "ca" / "0", "behaviors")) == 3
    assert read_table(root / "cb" / "0", "behaviors")["status_name"].tolist() == [
        "RUNNING", "FAILURE"]


def test_extra_predicate_composes_with_the_scope(tmp_path):
    root = _campaign(tmp_path)
    rows = read_table(root / "ca", "behaviors", where="status_name = ?", params=("SUCCESS",))
    assert rows["run_id"].tolist() == [0, 1]


def test_read_runs_carries_the_scenario_parameters(tmp_path):
    root = _campaign(tmp_path)
    assert read_runs(root / "ca")["param_speed"].tolist() == [1.0, 1.0]


def test_list_tables_hides_the_ingest_bookkeeping(tmp_path):
    assert list_tables(_campaign(tmp_path)) == ["behaviors", "runs"]


def test_table_info_reports_columns_and_is_empty_for_an_absent_table(tmp_path):
    root = _campaign(tmp_path)
    assert table_info(root, "runs")["param_speed"] == "REAL"
    assert table_info(root, "poses") == {}


def test_a_campaign_without_results_names_the_remedy(tmp_path):
    root = _campaign(tmp_path, with_data_db=False)
    with pytest.raises(CampaignDataError) as excinfo:
        read_table(root, "behaviors")
    message = str(excinfo.value)
    assert "no _execution/data.db" in message
    assert "postprocess" in message


def test_a_missing_table_lists_what_is_there(tmp_path):
    root = _campaign(tmp_path)
    with pytest.raises(CampaignDataError) as excinfo:
        read_table(root, "poses")
    assert "behaviors, runs" in str(excinfo.value)


def test_a_missing_behaviours_table_names_its_cause(tmp_path):
    """How a passing run ends up with no scenario tree, which is silent by construction.

    One cause rather than two since behaviour-tree logging stopped being optional: an
    execution image whose scenario_execution predates --bt-log drops the flag instead of
    refusing it, so the run passes and writes nothing.
    """
    root = _campaign(tmp_path, behaviours=False)
    with pytest.raises(CampaignDataError) as excinfo:
        read_table(root, "behaviors")
    assert "--bt-log" in str(excinfo.value)


def test_a_required_column_that_is_gone_raises_instead_of_returning_less(tmp_path):
    """A layout change must not read as a frame that is quietly missing a column."""
    root = _campaign(tmp_path)
    with pytest.raises(CampaignDataError) as excinfo:
        read_table(root, "behaviors", require=["status_name", "tip_id"])
    message = str(excinfo.value)
    assert "'tip_id'" in message
    assert "status_name" in message


def test_an_older_layout_still_reads_but_says_so_when_something_is_missing(tmp_path):
    """The version is advisory: what gates a query is whether the columns are there.

    Refusing every older database would lock out campaigns that carry everything the
    notebook needs -- which is most of them, since data.db predates the stamp.
    """
    root = _campaign(tmp_path, version=0)
    assert len(read_table(root, "behaviors")) == len(BEHAVIOURS)
    with pytest.raises(CampaignDataError) as excinfo:
        read_table(root, "behaviors", require=["tip_id"])
    assert "older layout (v0" in str(excinfo.value)


def test_a_newer_layout_is_read_rather_than_refused(tmp_path):
    root = _campaign(tmp_path, version=DATA_DB_SCHEMA_VERSION + 1)
    assert len(read_table(root, "behaviors")) == len(BEHAVIOURS)


def test_a_path_outside_a_campaign_says_so(tmp_path):
    with pytest.raises(CampaignDataError, match="not inside a campaign directory"):
        run_scope(tmp_path)


def test_a_non_numeric_run_directory_is_rejected(tmp_path):
    root = _campaign(tmp_path)
    (root / "ca" / "_config").mkdir()
    with pytest.raises(CampaignDataError, match="numeric run directory"):
        run_scope(root / "ca" / "_config")


def test_get_behavior_info_groups_by_the_sqlite_run_keys(tmp_path):
    """The frame from data.db is keyed (config_name, run_id), not (config, run)."""
    root = _campaign(tmp_path)
    info = get_behavior_info("nav", read_table(root, "behaviors"))
    assert set(info.columns) == {"behavior_name", "id", "start_time", "end_time",
                                 "duration", "config_name", "run_id"}
    durations = {(r.config_name, r.run_id): r.duration for r in info.itertuples()}
    assert durations == {("ca", 0): 3.0, ("ca", 1): 8.0, ("cb", 0): 3.0}


def test_get_behavior_info_still_groups_by_the_file_reader_keys(tmp_path):
    """read_output_files attaches (config, run); both spellings stay supported."""
    root = _campaign(tmp_path)
    frame = read_table(root, "behaviors").rename(
        columns={"config_name": "config", "run_id": "run"})
    info = get_behavior_info("nav", frame)
    assert set(info.columns) == {"behavior_name", "id", "start_time", "end_time",
                                 "duration", "config", "run"}
    assert len(info) == 3


def test_an_absent_behaviour_returns_the_same_columns_as_a_present_one(tmp_path):
    """The empty result used to drop start_time/end_time, so selecting them worked until
    the day the behaviour was not found."""
    root = _campaign(tmp_path)
    frame = read_table(root, "behaviors")
    assert list(get_behavior_info("nope", frame).columns) == list(
        get_behavior_info("nav", frame).columns)


def test_an_instance_that_never_finished_is_not_given_a_duration(tmp_path):
    """RUNNING with no terminal status has no duration, and inventing one would be worse."""
    root = _campaign(tmp_path)
    conn = sqlite3.connect(root / "_execution" / "data.db")
    conn.execute("INSERT INTO behaviors VALUES ('cb', 1, 1.0, 'nav', 'b1', 'RUNNING')")
    conn.commit()
    conn.close()
    info = get_behavior_info("nav", read_table(root, "behaviors"))
    assert ("cb", 1) not in {(r.config_name, r.run_id) for r in info.itertuples()}


def test_with_params_attaches_each_scenario_parameter_as_a_column(tmp_path):
    """What a metric varied with lives in `runs`, not on the metric table."""
    root = _campaign(tmp_path)
    frame = read_table(root, "behaviors", with_params=True)
    assert len(frame) == len(BEHAVIOURS)
    assert frame.loc[frame["config_name"] == "cb", "speed"].unique().tolist() == [2.0]
    assert "param_speed" not in frame.columns


def test_attach_params_refuses_to_shadow_a_measured_column(tmp_path):
    """A configured value quietly overwriting a measured one reads as a plausible plot."""
    root = _campaign(tmp_path)
    conn = sqlite3.connect(root / "_execution" / "data.db")
    conn.execute("ALTER TABLE runs RENAME COLUMN param_speed TO param_timestamp")
    conn.commit()
    conn.close()
    with pytest.raises(CampaignDataError, match="would overwrite"):
        read_table(root, "behaviors", with_params=True)


def test_attach_params_needs_the_run_keys(tmp_path):
    root = _campaign(tmp_path)
    frame = read_table(root, "behaviors").drop(columns=["run_id"])
    with pytest.raises(CampaignDataError, match="no \\['config_name', 'run_id'\\]"):
        attach_params(frame, root)


def test_config_file_resolves_against_the_campaign_snapshot(tmp_path):
    """map_file is relative to the campaign's _config/, at every scope."""
    root = _campaign(tmp_path)
    target = root / "_config" / "environments" / "hexagon" / "maps" / "hexagon.yaml"
    target.parent.mkdir(parents=True)
    target.touch()
    rel = "environments/hexagon/maps/hexagon.yaml"
    for node in (root, root / "ca", root / "ca" / "0"):
        assert config_file(node, rel) == target


def test_config_file_prefers_a_per_configuration_copy(tmp_path):
    root = _campaign(tmp_path)
    for base in (root / "_config", root / "ca" / "_config"):
        (base / "maps").mkdir(parents=True)
        (base / "maps" / "m.yaml").touch()
    assert config_file(root / "ca", "maps/m.yaml", "ca") == root / "ca" / "_config" / "maps" / "m.yaml"


def test_config_file_says_what_it_looked_for(tmp_path):
    root = _campaign(tmp_path)
    with pytest.raises(CampaignDataError, match="Looked at"):
        config_file(root, "maps/missing.yaml")


def test_config_file_can_hand_back_a_candidate_for_a_caller_that_probes(tmp_path):
    root = _campaign(tmp_path)
    path = config_file(root, "maps/missing.yaml", must_exist=False)
    assert path == root / "_config" / "maps" / "missing.yaml"
    assert not path.is_file()
