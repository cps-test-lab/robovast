# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Read-only SQL over a campaign's directory: its tables, built on first use, and its record.

A campaign is named by its directory; the tables a statement names are built from the
records in it the first time, and answered by an in-process engine over the files.
"""

import statistics

import pytest

from robovast.results_processing.data_query import (DataQueryError, _cap_cell,
                                                    describe_data_db, query_data_db)

# -- the cell cap, which needs no database ----------------------------------


def test_a_blob_cell_is_masked_rather_than_returned():
    """Bytes in a reply are unreadable and enormous; the length is the useful part."""
    capped = _cap_cell(b"\x80\x04" + b"\x00" * 4096)
    assert capped == "<BLOB 4098 bytes>"
    assert len(repr(capped)) < 4096


def test_an_oversized_text_cell_is_truncated_and_says_so():
    """Silently truncating would make a clipped value read as the whole value."""
    capped = _cap_cell("x" * 10_000)
    assert len(capped.encode()) < 10_000
    assert "truncated" in capped and "10000 chars total" in capped


def test_a_reasonable_cell_is_returned_untouched():
    assert _cap_cell("fine") == "fine"
    assert _cap_cell(1.5) == 1.5
    assert _cap_cell(None) is None


# -- JSON over a non-scalar param -------------------------------------------


def test_a_list_valued_param_can_be_unnested_and_filtered_in_sql(campaign_dir):
    """A spatial filter over a list-valued param is a query, not Python.

    Non-scalar scenario params are JSON-encoded in ``runs``, so a question over e.g.
    ``param_waypoints`` depends on the value being unnestable in SQL.
    """
    # Any waypoint within radius 1.5 of (1,2): cfg-a rows match, cfg-b (9,9) do not.
    sql = (
        "SELECT DISTINCT config_name FROM ("
        "  SELECT config_name, unnest(CAST(param_waypoints AS JSON[])) AS wp FROM runs) "
        "WHERE (CAST(wp->>'x' AS DOUBLE) - 1.0) ^ 2 + (CAST(wp->>'y' AS DOUBLE) - 2.0) ^ 2 "
        "    <= 1.5 * 1.5 "
        "ORDER BY config_name"
    )
    result = query_data_db(campaign_dir, sql)
    assert [row["config_name"] for row in result["rows"]] == ["cfg-a"]


def test_a_scalar_reached_through_a_json_path(campaign_dir):
    result = query_data_db(
        campaign_dir,
        "SELECT CAST(param_waypoints->0->>'x' AS DOUBLE) AS x0 "
        "FROM runs WHERE config_name = 'cfg-b' LIMIT 1",
    )
    assert result["rows"][0]["x0"] == 9.0


# -- statistical aggregates --------------------------------------------------


def test_stddev_aggregate(campaign_dir):
    result = query_data_db(
        campaign_dir,
        "SELECT STDDEV(CAST(error AS REAL)) AS s FROM landing_error "
        "WHERE config_name = 'cfg-a'",
    )
    assert result["rows"][0]["s"] == pytest.approx(statistics.stdev([0.10, 0.90]))


def test_median_aggregate(campaign_dir):
    result = query_data_db(
        campaign_dir, "SELECT MEDIAN(CAST(error AS REAL)) AS m FROM landing_error")
    assert result["rows"][0]["m"] == pytest.approx(statistics.median([0.1, 0.9, 0.2, 0.3]))


def test_percentile_aggregate(campaign_dir):
    result = query_data_db(
        campaign_dir, "SELECT PERCENTILE(CAST(error AS REAL), 50) AS p FROM landing_error")
    assert result["rows"][0]["p"] == pytest.approx(statistics.median([0.1, 0.9, 0.2, 0.3]))


# -- only a SELECT -------------------------------------------------------------


@pytest.mark.parametrize("sql", [
    "UPDATE runs SET status = 'x'",
    "CREATE TABLE t AS SELECT 1",
    "SELECT 1; SELECT 2",
    "COPY runs TO 'out.csv'",
])
def test_anything_but_one_select_is_refused(campaign_dir, sql):
    """Refused before it runs, and reported as this module's error type."""
    with pytest.raises(DataQueryError):
        query_data_db(campaign_dir, sql)
    assert query_data_db(
        campaign_dir, "SELECT COUNT(*) AS n FROM runs WHERE status = 'x'")["rows"] == [{"n": 0}]


def test_a_query_cannot_read_a_file_by_path(campaign_dir):
    """The views are the only way to data: a path in a statement is not a table."""
    with pytest.raises(DataQueryError):
        query_data_db(campaign_dir, f"SELECT * FROM read_csv('{campaign_dir}/campaign.db')")


# -- empty results -------------------------------------------------------------


def test_a_campaign_directory_that_does_not_exist_is_an_error_not_an_empty_result(
        campaign_dir):
    """"No rows" and "no such campaign" are different answers."""
    with pytest.raises(DataQueryError):
        query_data_db(campaign_dir.parent / "no-such-campaign", "SELECT * FROM runs")


def test_a_filter_that_matches_nothing_is_an_empty_result(campaign_dir):
    result = query_data_db(
        campaign_dir, "SELECT * FROM runs WHERE config_name = 'does-not-exist'")
    assert result["row_count"] == 0 and result["rows"] == []


# -- describe: aggregates + the campaign record ------------------------------


def test_describe_lists_runs_and_the_campaign_record(campaign_dir):
    """Both halves stay reachable: the measurements and the driver's record."""
    desc = describe_data_db(campaign_dir)
    schemas = {(t["schema"], t["table"]) for t in desc["tables"]}
    assert ("main", "runs") in schemas
    assert ("campaign", "campaign") in schemas


def test_describe_builds_nothing(campaign_dir):
    """The catalog says what can be built and for how many runs, without building it."""
    desc = describe_data_db(campaign_dir)
    landing = next(t for t in desc["tables"] if t["table"] == "landing_error")
    assert landing["built"] == 0 and landing["runs"] == 4
    assert not (campaign_dir / ".cache" / "tables").exists()


def test_describe_note_mentions_aggregates(campaign_dir):
    note = describe_data_db(campaign_dir)["note"].upper()
    assert "MEDIAN" in note or "STDDEV" in note or "PERCENTILE" in note


# -- the campaign record is reachable before any data file ---------------------


def test_describe_without_measurements_still_returns_the_campaign_schema(
        campaign_dir_no_data):
    desc = describe_data_db(campaign_dir_no_data)
    schemas = {(t["schema"], t["table"]) for t in desc["tables"]}
    assert ("campaign", "campaign") in schemas


def test_query_config_json_without_measurements(campaign_dir_no_data):
    result = query_data_db(
        campaign_dir_no_data,
        "SELECT CAST(config_json AS JSON)->'evaluation' AS ev FROM campaign.campaign")
    assert result["rows"][0]["ev"] is not None


# -- the campaign's one real blob --------------------------------------------


def test_the_strategy_blob_never_reaches_a_reply(campaign_dir):
    """A multi-KB pickle is not offered: nothing queryable can hand it back."""
    desc = describe_data_db(campaign_dir)
    record = next(t for t in desc["tables"]
                  if (t["schema"], t["table"]) == ("campaign", "campaign"))
    assert not any(c.startswith("strategy_state") for c in record["columns"])

    with pytest.raises(DataQueryError):
        query_data_db(campaign_dir, "SELECT strategy_state FROM campaign.campaign")
