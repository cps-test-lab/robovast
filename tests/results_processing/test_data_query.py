# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Tests for read-only SQL over a campaign's ``data.db`` (Phase 0/1)."""

import statistics

import pytest

from robovast.results_processing.data_query import DataQueryError, describe_data_db, query_data_db

# -- json_each survives the authorizer (Phase 0.2, gates search_metadata delete) --


def test_json_each_over_json_encoded_param_not_denied(campaign_dir):
    """``json_each`` must run under the read-only authorizer.

    Non-scalar scenario params are stored JSON-encoded, so list/spatial queries
    over e.g. ``param_waypoints`` depend on the ``json_each`` virtual table not
    being rejected by ``_readonly_authorizer``. This is the capability that lets
    the hand-rolled ``search_metadata`` spatial filters be replaced by SQL.
    """
    # Any waypoint within radius 1.5 of (1,2): cfg-a rows match, cfg-b (9,9) do not.
    sql = (
        "SELECT DISTINCT r.config_name FROM runs r, json_each(r.param_waypoints) je "
        "WHERE (json_extract(je.value,'$.x') - 1.0) * (json_extract(je.value,'$.x') - 1.0) "
        "    + (json_extract(je.value,'$.y') - 2.0) * (json_extract(je.value,'$.y') - 2.0) "
        "    <= 1.5 * 1.5 "
        "ORDER BY r.config_name"
    )
    result = query_data_db(campaign_dir, sql)
    assert [row["config_name"] for row in result["rows"]] == ["cfg-a"]


def test_json_extract_scalar_not_denied(campaign_dir):
    result = query_data_db(
        campaign_dir,
        "SELECT json_extract(param_waypoints,'$[0].x') AS x0 FROM runs WHERE config_name='cfg-b' LIMIT 1",
    )
    assert result["rows"][0]["x0"] == 9.0


# -- statistical aggregates (Phase 0.1) --------------------------------------


def test_stddev_aggregate(campaign_dir):
    result = query_data_db(
        campaign_dir,
        "SELECT STDDEV(CAST(error AS REAL)) AS s FROM landing_error WHERE config_name='cfg-a'",
    )
    expected = statistics.stdev([0.10, 0.90])
    assert result["rows"][0]["s"] == pytest.approx(expected)


def test_median_aggregate(campaign_dir):
    result = query_data_db(
        campaign_dir,
        "SELECT MEDIAN(CAST(error AS REAL)) AS m FROM landing_error",
    )
    assert result["rows"][0]["m"] == pytest.approx(statistics.median([0.1, 0.9, 0.2, 0.3]))


def test_percentile_aggregate(campaign_dir):
    result = query_data_db(
        campaign_dir,
        "SELECT PERCENTILE(CAST(error AS REAL), 50) AS p FROM landing_error",
    )
    assert result["rows"][0]["p"] == pytest.approx(statistics.median([0.1, 0.9, 0.2, 0.3]))


# -- write queries stay rejected (regression guard for the aggregates change) --


def test_write_query_rejected(campaign_dir):
    with pytest.raises(DataQueryError):
        query_data_db(campaign_dir, "UPDATE runs SET status='x'")


# -- empty-result disambiguation (Phase 0.3) ---------------------------------


def test_empty_result_carries_disambiguating_note(campaign_dir):
    """A 0-row result over a populated table should say so, not look like 'no data'."""
    result = query_data_db(
        campaign_dir,
        "SELECT * FROM runs WHERE config_name='does-not-exist'",
    )
    assert result["row_count"] == 0
    assert "note" in result
    # The note should reveal that the referenced table is NOT itself empty.
    assert "runs" in result["note"]


# -- describe: aggregates + attached-table semantics (Phase 0.1 / 1.4a) ------


def test_describe_lists_runs_and_campaign(campaign_dir):
    desc = describe_data_db(campaign_dir)
    schemas = {(t["schema"], t["table"]) for t in desc["tables"]}
    assert ("main", "runs") in schemas
    assert ("campaign", "campaign") in schemas


def test_describe_note_mentions_aggregates(campaign_dir):
    desc = describe_data_db(campaign_dir)
    note = desc["note"].upper()
    assert "MEDIAN" in note or "STDDEV" in note or "PERCENTILE" in note


# -- campaign.db reachable without data.db (Phase 1.4b) ----------------------


def test_describe_without_data_db_still_returns_campaign_schema(campaign_dir_no_data):
    """Before postprocessing, describe must still expose campaign/batch/unit."""
    desc = describe_data_db(campaign_dir_no_data)
    schemas = {(t["schema"], t["table"]) for t in desc["tables"]}
    assert ("campaign", "campaign") in schemas


def test_query_config_json_without_data_db(campaign_dir_no_data):
    result = query_data_db(
        campaign_dir_no_data,
        "SELECT json_extract(config_json,'$.evaluation') AS ev FROM campaign.campaign",
    )
    assert result["rows"][0]["ev"] is not None


# -- byte cap / blob masking (Phase 1.4c) ------------------------------------


def test_large_blob_is_capped(campaign_dir):
    """A multi-KB BLOB cell must not be returned verbatim into the response."""
    result = query_data_db(campaign_dir, "SELECT strategy_state FROM campaign.campaign")
    cell = result["rows"][0]["strategy_state"]
    # However represented (masked/truncated), it must be far smaller than the raw blob.
    assert len(repr(cell)) < 4096
