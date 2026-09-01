# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Read-only SQL over a campaign's results, now served from the central index.

``data_query``'s public surface is unchanged -- a campaign is still named by its
directory -- but the rows come from Postgres, so what each test pins had to be restated
against that substrate rather than against ``data.db``:

* JSON over a non-scalar param is ``jsonb``, not SQLite's ``json_each``/``json_extract``.
  The capability being pinned is the same one: a list-valued scenario param can be
  unnested and filtered *in SQL*, which is what lets the hand-rolled ``search_metadata``
  spatial filters be a query.
* Read-only is a session setting the server enforces, not SQLite's C-level authorizer.
* The oversized-cell guard is client-side and unchanged; the campaign's one real blob
  (``strategy_state``) never reaches the index at all now.

Anything that reads rows needs Postgres and skips without ``ROBOVAST_TEST_PG_DSN``;
:func:`_cap_cell` is pure arithmetic and is tested unconditionally.
"""

import statistics

import pytest

from robovast.results_processing.data_query import (DataQueryError, _cap_cell,
                                                    describe_data_db, query_data_db)

from .conftest import SCHEMA

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
    """The spatial filter has to be expressible as a query, not as Python.

    Non-scalar scenario params are stored JSON-encoded, so list/spatial questions over
    e.g. ``param_waypoints`` depend on the row being unnestable in SQL --
    ``jsonb_array_elements`` here, where ``data.db`` had ``json_each``. This is the
    capability that lets the hand-rolled ``search_metadata`` spatial filters be replaced
    by SQL.
    """
    # Any waypoint within radius 1.5 of (1,2): cfg-a rows match, cfg-b (9,9) do not.
    sql = (
        "SELECT DISTINCT r.config_name FROM runs r, "
        "     jsonb_array_elements(r.param_waypoints::jsonb) je "
        "WHERE ((je->>'x')::double precision - 1.0) * ((je->>'x')::double precision - 1.0) "
        "    + ((je->>'y')::double precision - 2.0) * ((je->>'y')::double precision - 2.0) "
        "    <= 1.5 * 1.5 "
        "ORDER BY r.config_name"
    )
    result = query_data_db(campaign_dir, sql)
    assert [row["config_name"] for row in result["rows"]] == ["cfg-a"]


def test_a_scalar_reached_through_a_json_path(campaign_dir):
    result = query_data_db(
        campaign_dir,
        "SELECT (param_waypoints::jsonb -> 0 ->> 'x')::double precision AS x0 "
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
    """Refused by the read-only session, and reported as this module's error type.

    SQLite vetted every action through its authorizer; Postgres has no such hook, so the
    guarantee now comes from ``default_transaction_read_only``. The caller-visible
    contract is what matters here and it is unchanged: a write raises ``DataQueryError``
    and the rows are still there afterwards.
    """
    with pytest.raises(DataQueryError):
        query_data_db(campaign_dir, "UPDATE runs SET status = 'x'")

    assert query_data_db(
        campaign_dir, "SELECT COUNT(*) AS n FROM runs WHERE status = 'x'"
    )["rows"] == [{"n": 0}]


# -- empty-result disambiguation --------------------------------------------


def test_an_empty_result_from_a_campaign_that_was_never_ingested_says_so(campaign_dir):
    """"No rows" and "no such campaign" are different answers.

    ``data.db``'s version of this listed the non-empty base tables, because the only way
    to get nothing from an existing file was a filter or JOIN-key mismatch. In one shared
    index the likelier cause is that the campaign was never ingested -- an empty result
    set claims the opposite -- so that is what the note has to separate.
    """
    result = query_data_db(
        campaign_dir.parent / "never-ingested",
        "SELECT * FROM runs WHERE campaign_id = 'never-ingested'",
    )
    assert result["row_count"] == 0
    assert "not in the index" in result["note"]


def test_an_empty_result_from_a_real_campaign_is_not_mislabelled(campaign_dir):
    """The other direction: an ingested campaign with a filter that matches nothing."""
    result = query_data_db(
        campaign_dir, "SELECT * FROM runs WHERE config_name = 'does-not-exist'")
    assert result["row_count"] == 0
    assert "not in the index" not in result.get("note", "")


# -- describe: aggregates + the campaign record ------------------------------


def test_describe_lists_runs_and_the_campaign_record(campaign_dir):
    """Both halves stay reachable: the measurements and the driver's record.

    The record is mirrored into the ``campaign`` schema rather than attached from
    ``campaign.db``, so the qualified name a caller writes is the same one.
    """
    desc = describe_data_db(campaign_dir)
    schemas = {(t["schema"], t["table"]) for t in desc["tables"]}
    assert (SCHEMA, "runs") in schemas
    assert ("campaign", "campaign") in schemas


def test_describe_note_mentions_aggregates(campaign_dir):
    desc = describe_data_db(campaign_dir)
    note = desc["note"].upper()
    assert "MEDIAN" in note or "STDDEV" in note or "PERCENTILE" in note


# -- the campaign record is reachable before postprocessing ------------------


def test_describe_without_measurements_still_returns_the_campaign_schema(
        campaign_dir_no_data):
    """Before postprocessing, describe must still expose the campaign record."""
    desc = describe_data_db(campaign_dir_no_data)
    schemas = {(t["schema"], t["table"]) for t in desc["tables"]}
    assert ("campaign", "campaign") in schemas


def test_query_config_json_without_measurements(campaign_dir_no_data):
    result = query_data_db(
        campaign_dir_no_data,
        "SELECT config_json::jsonb -> 'evaluation' AS ev FROM campaign.campaign")
    assert result["rows"][0]["ev"] is not None


# -- the campaign's one real blob --------------------------------------------


def test_the_strategy_blob_never_reaches_a_reply(campaign_dir):
    """A multi-KB pickle must not be returned verbatim into the response.

    ``data.db`` mirrored it and the reply masked it per cell. The index does not mirror
    it at all (``dimension_ingest._EXCLUDED_COLUMNS``), which is the stronger form of the
    same guarantee: nothing queryable can hand it back. The per-cell mask still exists
    for anything else oversized -- see the ``_cap_cell`` tests above.
    """
    desc = describe_data_db(campaign_dir)
    record = next(t for t in desc["tables"]
                  if (t["schema"], t["table"]) == ("campaign", "campaign"))
    assert not any(c.startswith("strategy_state") for c in record["columns"])

    with pytest.raises(DataQueryError):
        query_data_db(campaign_dir, "SELECT strategy_state FROM campaign.campaign")
