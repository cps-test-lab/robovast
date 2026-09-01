# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Reading a campaign's results from the central index, scoped to a notebook's DATA_DIR.

Split by what a test needs, because the split is the point:

* **The scoping logic runs without a database.** ``_scope_clause`` is where a campaign
  stops being a file and becomes a ``WHERE`` clause, and getting it wrong widens a
  notebook to the whole corpus with no error and nothing empty to notice. That is the one
  failure here worth pinning unconditionally, so those tests are ungated.
* **Everything that actually reads rows needs Postgres**, and skips on
  ``ROBOVAST_TEST_PG_DSN`` being unset -- same gate as
  ``tests/results_processing/test_index_matches_data_db.py``. Two campaigns are ingested
  rather than one: a scoping bug that only ever sees a single campaign is invisible.
"""

import csv
import os

import pandas as pd
import pytest

from robovast.common.analysis import (CampaignDataError, attach_params, campaign_root,
                                      config_file, get_behavior_info, list_tables, read_sql,
                                      read_table, run_scope, table_info)
from robovast.common.analysis import db as analysis_db

DSN = os.environ.get("ROBOVAST_TEST_PG_DSN")
SCHEMA = "analysis_db_test"

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

#: The other campaign in the index. Same configuration and run ids on purpose: those are
#: exactly the keys that collide once every campaign shares one table.
OTHER_BEHAVIOURS = [
    ("ca", 0, 0.0, "nav", "b1", "RUNNING"),
    ("ca", 0, 7.0, "nav", "b1", "FAILURE"),
]

_COLUMNS = ["config_name", "run_id", "timestamp", "behavior_name", "behavior_id",
            "status_name"]


def _behaviour_frame(rows=None) -> pd.DataFrame:
    """The frame ``read_table(..., "behaviors")`` returns, without needing an index.

    The metric helpers take a frame and know nothing about where it came from, so making
    them wait for a database would gate them on something they do not use.
    """
    return pd.DataFrame(list(BEHAVIOURS if rows is None else rows), columns=_COLUMNS)


def _campaign_tree(tmp_path, name, behaviours=BEHAVIOURS):
    """A campaign directory shaped like a real one: configs, runs, one CSV per run.

    No ``data.db`` any more -- the tree on disk is what a notebook's ``DATA_DIR`` points
    into, and the rows live in the index. ``_execution/`` is what makes it recognisable as
    a campaign root.
    """
    root = tmp_path / name
    (root / "_execution").mkdir(parents=True)
    per_run = {}
    for config, run, *rest in behaviours:
        per_run.setdefault((config, run), []).append(rest)
    for (config, run), rows in per_run.items():
        run_dir = root / config / str(run)
        run_dir.mkdir(parents=True, exist_ok=True)
        with (run_dir / "behaviors.csv").open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(_COLUMNS[2:])
            writer.writerows(rows)
    return root


# -- what a path selects, which needs no database ---------------------------

def test_campaign_root_is_found_from_every_node(tmp_path):
    root = _campaign_tree(tmp_path, "camp-2026-08-10-07150919")
    assert campaign_root(root) == root
    assert campaign_root(root / "ca") == root
    assert campaign_root(root / "ca" / "0") == root


def test_campaign_root_does_not_depend_on_the_directory_name(tmp_path):
    """A results directory gets renamed and copied; the campaign is still the campaign."""
    root = _campaign_tree(tmp_path, "camp-2026-08-10-07150919")
    renamed = tmp_path / "not-a-campaign-shaped-name"
    root.rename(renamed)
    assert campaign_root(renamed / "ca" / "0") == renamed


def test_run_scope_reads_the_three_levels(tmp_path):
    root = _campaign_tree(tmp_path, "camp-2026-08-10-07150919")
    assert run_scope(root) == ("campaign", None, None)
    assert run_scope(root / "ca") == ("config", "ca", None)
    assert run_scope(root / "ca" / "0") == ("run", "ca", 0)


def test_a_path_outside_a_campaign_says_so(tmp_path):
    with pytest.raises(CampaignDataError, match="not inside a campaign directory"):
        run_scope(tmp_path)


def test_a_non_numeric_run_directory_is_rejected(tmp_path):
    root = _campaign_tree(tmp_path, "camp-2026-08-10-07150919")
    (root / "ca" / "_config").mkdir()
    with pytest.raises(CampaignDataError, match="numeric run directory"):
        run_scope(root / "ca" / "_config")


def test_the_campaign_id_is_the_same_from_every_node_of_one_campaign(tmp_path):
    """It is the value every query filters on, so it must not depend on which node is open.

    A notebook opened on a run and one opened on the campaign root are reading the same
    campaign; if the id differed by depth, half the scoping predicates would match nothing.
    """
    root = _campaign_tree(tmp_path, "camp-2026-08-10-07150919")
    ids = {analysis_db.campaign_id(node)
           for node in (root, root / "ca", root / "ca" / "0")}
    assert ids == {"camp-2026-08-10-07150919"}


def test_the_campaign_id_follows_a_renamed_results_directory(tmp_path):
    """The directory name *is* the id, so a rename renames the campaign.

    Worth stating rather than assuming: a copied-and-renamed tree no longer names the rows
    that were ingested under the old name, and the failure is an empty frame. The remedy
    (re-ingest, or keep the name) only reaches anyone who knows this is the rule.
    """
    root = _campaign_tree(tmp_path, "camp-2026-08-10-07150919")
    renamed = tmp_path / "copied-elsewhere"
    root.rename(renamed)
    assert analysis_db.campaign_id(renamed / "ca" / "0") == "copied-elsewhere"


# -- the scope clause, which is what stops a notebook reading the corpus ------

KEYED = {"campaign_id": "text", "config_name": "text", "run_id": "bigint",
         "value": "double precision"}


def test_the_campaign_scope_still_filters_on_the_campaign():
    """The campaign level used to mean "no filter", because the file was the campaign.

    It now has to mean the opposite. Every campaign shares one index, so an unfiltered
    read at the campaign level returns the whole corpus -- as an entirely ordinary frame,
    with no error and nothing empty to notice. This is the predicate that prevents it.
    """
    clause, values = _scope("camp-1", "campaign", None, None)

    assert clause == "WHERE campaign_id = %s"
    assert values == ["camp-1"]


def test_a_configuration_scope_narrows_within_the_campaign_rather_than_replacing_it():
    """Narrowing must be additive: a configuration name is not unique across campaigns.

    Every campaign has a 'nominal', so filtering on the configuration alone would return
    one row set per campaign that happens to use the name.
    """
    clause, values = _scope("camp-1", "config", "nominal", None)

    assert clause == "WHERE campaign_id = %s AND config_name = %s"
    assert values == ["camp-1", "nominal"]


def test_a_run_scope_carries_all_three_keys_in_placeholder_order():
    """Run 0 of 'nominal' exists in nearly every campaign; only all three keys pick one."""
    clause, values = _scope("camp-1", "run", "nominal", 3)

    assert clause == "WHERE campaign_id = %s AND config_name = %s AND run_id = %s"
    assert values == ["camp-1", "nominal", 3]


@pytest.mark.parametrize("level,config,run", [("campaign", None, None),
                                              ("config", "nominal", None),
                                              ("run", "nominal", 3)])
def test_every_scope_binds_its_values_rather_than_interpolating_them(level, config, run):
    """One placeholder per value, in Postgres' spelling.

    ``?`` is not a parameter marker in Postgres -- it is a syntax error -- and a count
    mismatch is how a scope silently binds the campaign id to the configuration column.
    """
    clause, values = _scope("camp-1", level, config, run)

    assert "?" not in clause
    assert clause.count("%s") == len(values)


def test_a_table_the_ingest_does_not_key_by_run_is_still_kept_to_this_campaign():
    """A campaign-wide table has no run keys, and narrowing it would be an error.

    But it is still one campaign's, so the campaign predicate stays: the alternative is a
    corpus-wide read of exactly the tables a notebook summarises without joining.
    """
    clause, values = _scope("camp-1", "run", "nominal", 3,
                            columns={"campaign_id": "text", "note": "text"})

    assert clause == "WHERE campaign_id = %s"
    assert values == ["camp-1"]


def test_a_table_with_none_of_the_scope_columns_is_read_whole():
    """Nothing to filter on means no clause -- an empty predicate, not a broken one."""
    clause, values = _scope("camp-1", "run", "nominal", 3,
                            columns={"note": "text"})

    assert (clause, values) == ("", [])


def _scope(campaign, level, config_name, run_id, columns=None):
    return analysis_db._scope_clause(  # pylint: disable=protected-access
        campaign, level, config_name, run_id, KEYED if columns is None else columns)


# -- what a frame means, which the metric helpers answer ---------------------

def test_get_behavior_info_groups_by_the_run_keys_the_index_uses():
    """The frame from the index is keyed (config_name, run_id), not (config, run)."""
    info = get_behavior_info("nav", _behaviour_frame())
    assert set(info.columns) == {"behavior_name", "id", "start_time", "end_time",
                                 "duration", "config_name", "run_id"}
    durations = {(r.config_name, r.run_id): r.duration for r in info.itertuples()}
    assert durations == {("ca", 0): 3.0, ("ca", 1): 8.0, ("cb", 0): 3.0}


def test_get_behavior_info_still_groups_by_the_file_reader_keys():
    """read_output_files attaches (config, run); both spellings stay supported."""
    frame = _behaviour_frame().rename(columns={"config_name": "config", "run_id": "run"})
    info = get_behavior_info("nav", frame)
    assert set(info.columns) == {"behavior_name", "id", "start_time", "end_time",
                                 "duration", "config", "run"}
    assert len(info) == 3


def test_an_absent_behaviour_returns_the_same_columns_as_a_present_one():
    """An empty result that drops start_time/end_time makes selecting them work until the
    day the behaviour is not found."""
    frame = _behaviour_frame()
    assert list(get_behavior_info("nope", frame).columns) == list(
        get_behavior_info("nav", frame).columns)


def test_an_instance_that_never_finished_is_not_given_a_duration():
    """RUNNING with no terminal status has no duration, and inventing one would be worse."""
    frame = _behaviour_frame(
        [*BEHAVIOURS, ("cb", 1, 1.0, "nav", "b1", "RUNNING")])
    info = get_behavior_info("nav", frame)
    assert ("cb", 1) not in {(r.config_name, r.run_id) for r in info.itertuples()}


def test_attach_params_needs_the_run_keys():
    """Refused before any query runs: a join key that is not there cannot be guessed."""
    frame = _behaviour_frame().drop(columns=["run_id"])
    with pytest.raises(CampaignDataError, match="no \\['config_name', 'run_id'\\]"):
        attach_params(frame, "/nowhere")


# -- the campaign snapshot on disk, which the index does not hold ------------

def test_config_file_resolves_against_the_campaign_snapshot(tmp_path):
    """map_file is relative to the campaign's _config/, at every scope."""
    root = _campaign_tree(tmp_path, "camp-2026-08-10-07150919")
    target = root / "_config" / "environments" / "hexagon" / "maps" / "hexagon.yaml"
    target.parent.mkdir(parents=True)
    target.touch()
    rel = "environments/hexagon/maps/hexagon.yaml"
    for node in (root, root / "ca", root / "ca" / "0"):
        assert config_file(node, rel) == target


def test_config_file_prefers_a_per_configuration_copy(tmp_path):
    root = _campaign_tree(tmp_path, "camp-2026-08-10-07150919")
    for base in (root / "_config", root / "ca" / "_config"):
        (base / "maps").mkdir(parents=True)
        (base / "maps" / "m.yaml").touch()
    assert config_file(root / "ca", "maps/m.yaml", "ca") == (
        root / "ca" / "_config" / "maps" / "m.yaml")


def test_config_file_says_what_it_looked_for(tmp_path):
    root = _campaign_tree(tmp_path, "camp-2026-08-10-07150919")
    with pytest.raises(CampaignDataError, match="Looked at"):
        config_file(root, "maps/missing.yaml")


def test_config_file_can_hand_back_a_candidate_for_a_caller_that_probes(tmp_path):
    root = _campaign_tree(tmp_path, "camp-2026-08-10-07150919")
    path = config_file(root, "maps/missing.yaml", must_exist=False)
    assert path == root / "_config" / "maps" / "missing.yaml"
    assert not path.is_file()


# -- reading rows, which needs the index ------------------------------------

pg = pytest.mark.skipif(not DSN, reason="ROBOVAST_TEST_PG_DSN is not set")

THIS = "camp-a-2026-08-10-07150919"
OTHER = "camp-b-2026-08-10-07150920"


@pytest.fixture(name="campaigns", scope="module")
def _campaigns(tmp_path_factory):
    """Two ingested campaigns, returned as ``(this_root, other_root)``.

    Two, because every table now holds every campaign and they share configuration names
    and run ids. A single-campaign fixture cannot tell a correctly scoped read from an
    unscoped one -- both return the same rows.
    """
    psycopg = pytest.importorskip("psycopg")
    from robovast.results_processing import campaign_ingest, index_query, index_views

    previous = os.environ.get("ROBOVAST_INDEX_DSN")
    os.environ["ROBOVAST_INDEX_DSN"] = f"{DSN} options=-csearch_path={SCHEMA}"
    with psycopg.connect(DSN, autocommit=True) as setup:
        for statement in (f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE",
                          "DROP SCHEMA IF EXISTS campaign CASCADE",
                          f"CREATE SCHEMA {SCHEMA}"):
            setup.execute(statement)

    base = tmp_path_factory.mktemp("campaigns")
    this = _campaign_tree(base, THIS)
    other = _campaign_tree(base, OTHER, behaviours=OTHER_BEHAVIOURS)
    with index_query.open_index(readonly=False) as conn:
        campaign_ingest.ingest_campaign(conn, str(this), THIS)
        campaign_ingest.ingest_campaign(conn, str(other), OTHER)
        index_views.create_views(conn)

    yield this, other

    with psycopg.connect(DSN, autocommit=True) as teardown:
        teardown.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        teardown.execute("DROP SCHEMA IF EXISTS campaign CASCADE")
    if previous is None:
        os.environ.pop("ROBOVAST_INDEX_DSN", None)
    else:
        os.environ["ROBOVAST_INDEX_DSN"] = previous


@pg
def test_reading_narrows_to_the_selected_node(campaigns):
    this, _ = campaigns
    assert len(read_table(this, "behaviors")) == len(BEHAVIOURS)
    assert len(read_table(this / "ca", "behaviors")) == 5
    assert len(read_table(this / "ca" / "0", "behaviors")) == 3
    assert read_table(this / "cb" / "0", "behaviors")["status_name"].tolist() == [
        "RUNNING", "FAILURE"]


@pg
def test_another_campaigns_rows_never_reach_this_campaigns_frame(campaigns):
    """The failure this module's scoping exists to prevent, at every level.

    Both campaigns have a config 'ca' with a run 0, so an unscoped read returns a frame
    that is the right shape, the right columns and the wrong experiment. Nothing raises
    and nothing is empty; the only symptom is a number.
    """
    this, _ = campaigns
    for node in (this, this / "ca", this / "ca" / "0"):
        frame = read_table(node, "behaviors")
        assert frame["campaign_id"].unique().tolist() == [THIS]


@pg
def test_each_campaign_reads_its_own_rows_from_the_same_table(campaigns):
    """Both directions, so a scope pinned to the wrong constant would still fail."""
    this, other = campaigns
    assert len(read_table(this, "behaviors")) == len(BEHAVIOURS)
    assert len(read_table(other, "behaviors")) == len(OTHER_BEHAVIOURS)


@pg
def test_an_extra_predicate_composes_with_the_scope_rather_than_replacing_it(campaigns):
    """A caller's own ``WHERE`` must not be able to widen the read back to the corpus."""
    this, _ = campaigns
    rows = read_table(this / "ca", "behaviors", where="status_name = %s",
                      params=("SUCCESS",))
    assert rows["run_id"].tolist() == [0, 1]
    assert rows["campaign_id"].unique().tolist() == [THIS]


@pg
def test_read_sql_binds_this_campaigns_id_for_the_caller(campaigns):
    """The id is always available as %(campaign_id)s, so scoping costs a clause not a lookup.

    Without it every hand-written query would have to rediscover which campaign the
    notebook is open on, and the version that skips the step still returns a frame.
    """
    this, _ = campaigns
    frame = read_sql(this, "SELECT * FROM behaviors WHERE campaign_id = %(campaign_id)s")
    assert len(frame) == len(BEHAVIOURS)
    assert frame["campaign_id"].unique().tolist() == [THIS]


@pg
def test_read_sql_left_unfiltered_still_reads_only_this_campaign(campaigns):
    """The hazard this used to document, removed rather than described.

    ``read_sql`` is the escape hatch, and a notebook cell that forgets the predicate used
    to read the corpus -- as an ordinary frame, of the right shape, plotted under this
    campaign's title. The session is confined to the campaign the notebook was opened on,
    so the omission is no longer the difference between one experiment and all of them.
    Spanning campaigns is still possible; it is asked for (see
    ``index_query.open_index(campaigns=[...])``).
    """
    this, _ = campaigns
    frame = read_sql(this, "SELECT * FROM behaviors")
    assert set(frame["campaign_id"]) == {THIS}


@pg
def test_table_info_reports_the_columns_a_notebook_would_select(campaigns):
    this, _ = campaigns
    info = table_info(this, "behaviors")
    assert "status_name" in info
    assert set(info) >= {"campaign_id", "config_name", "run_id", "timestamp"}


@pg
def test_table_info_is_empty_for_a_table_the_index_does_not_have(campaigns):
    """A notebook that adapts to what a campaign carries needs an answer, not an error."""
    this, _ = campaigns
    assert table_info(this, "poses") == {}


@pg
def test_a_missing_table_lists_what_is_there(campaigns):
    this, _ = campaigns
    with pytest.raises(CampaignDataError) as excinfo:
        read_table(this, "poses")
    assert "behaviors" in str(excinfo.value)


@pg
def test_list_tables_hides_the_ingest_bookkeeping(campaigns):
    """``_table_name_map`` and the schema registry are the ingest's, not the campaign's."""
    this, _ = campaigns
    names = list_tables(this)
    assert "behaviors" in names
    assert not [n for n in names if str(n).startswith("_")]


@pg
def test_a_required_column_that_is_gone_raises_instead_of_returning_less(campaigns):
    """A layout change must not read as a frame that is quietly missing a column."""
    this, _ = campaigns
    with pytest.raises(CampaignDataError) as excinfo:
        read_table(this, "behaviors", require=["status_name", "tip_id"])
    message = str(excinfo.value)
    assert "'tip_id'" in message
    assert "status_name" in message


@pg
def test_a_campaign_that_was_never_ingested_names_the_remedy(tmp_path, campaigns):
    """Not-ingested and ingested-but-empty are different answers, and only one is a result.

    One index holds every campaign, so a campaign whose postprocessing never ran returns
    zero rows exactly as one that genuinely measured nothing does. Plotted, the first
    reads as the second -- a claim about the experiment made by a gap in the pipeline.
    """
    del campaigns  # for the ingested schema and its DSN, not for its rows
    absent = _campaign_tree(tmp_path, "camp-never-ingested-2026-08-10-07150999")
    with pytest.raises(CampaignDataError) as excinfo:
        read_table(absent, "behaviors")
    assert "not in the index" in str(excinfo.value)


@pg
def test_a_campaign_with_no_behaviour_tree_still_names_its_cause(tmp_path, campaigns):
    """How a passing run ends up with no scenario tree, which is silent by construction.

    An execution image whose scenario_execution predates --bt-log drops the flag instead
    of refusing it, so the run passes and writes nothing. Nothing else in the results says
    so, which is why this error message is the only place a reader can learn it.

    The campaign here is ingested and has rows -- just not *these* rows. That is the case a
    shared index makes easy to get wrong: ``behaviors`` exists as a table, because the other
    campaigns in the fixture have one, so a check that asks whether the table exists would
    hand this campaign an empty frame and no explanation.
    """
    from robovast.common import index_db
    from robovast.results_processing import campaign_ingest

    del campaigns  # for the ingested schema and its DSN, not for its rows
    name = "camp-no-bt-2026-08-10-07150921"
    root = tmp_path / name
    run_dir = root / "nominal" / "0"
    run_dir.mkdir(parents=True)
    (root / "_execution").mkdir()
    (run_dir / "poses.csv").write_text(
        "timestamp,frame_id,x\n1.0,base_link,0.5\n", encoding="utf-8")
    with index_db.connect() as conn:
        campaign_ingest.ingest_campaign(conn, str(root), name)

    with pytest.raises(CampaignDataError) as excinfo:
        read_table(root, "behaviors")
    message = str(excinfo.value)
    assert "--bt-log" in message
    assert "poses" in message, "the error should name what this campaign does have"
