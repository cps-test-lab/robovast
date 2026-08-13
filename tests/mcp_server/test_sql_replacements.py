# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The queries that replaced the ``metadata.yaml`` tools must keep working.

Nine tools were deleted in favour of SQL, and the replacement for each is written into
``_TABLE_DESCRIPTIONS`` / ``_DESCRIBE_NOTE`` so a caller does not have to derive it. That
makes those queries part of the interface: a documented query that silently stopped
matching the schema is the same defect as the retired file route that looked documented
while matching no directory on disk.

So each is executed here against a fixture campaign, and the two properties the views
exist for are pinned: no cross-config bleed, and no truncated config.
"""

import json
import sqlite3

import pytest
import yaml

from robovast.common.campaign_data import read_run_outcome
from robovast.common.store import STORE_FILENAME, CampaignStore
from robovast.results_processing.data_query import (_MAX_CELL_BYTES, describe_data_db,
                                                    query_data_db)

_SYSINFO = {"cpu_name": "Intel Xeon", "available_cpus": 4, "instance_type": "n1"}


@pytest.fixture
def campaign(tmp_path):
    """Two configs × two runs, sharing one job, with a .vast well over the cell cap."""
    # A config big enough that `SELECT config_json` would be truncated — the case
    # config_view exists for.
    config = {"execution": {"containers": {"scenario": {"image": "sim:1"}}, "runs": 2},
              "notes": ["padding item %d" % i for i in range(400)]}
    assert len(json.dumps(config)) > _MAX_CELL_BYTES

    job = tmp_path / "_jobs" / "batch-0" / "job-0"
    job.mkdir(parents=True)
    (job / "sysinfo.yaml").write_text(yaml.dump(_SYSINFO), encoding="utf-8")

    with CampaignStore(tmp_path / STORE_FILENAME) as store:
        cid = store.create_campaign("c", config, mode="batch", config_dir="_config")
        bid = store.open_batch(cid, 0, ".")
        for cfg, speed in (("cfg-a", 1.0), ("cfg-b", 2.0)):
            for run in (0, 1):
                run_dir = tmp_path / cfg / str(run)
                run_dir.mkdir(parents=True)
                # cfg-a passes both runs; cfg-b fails run 1.
                failures = 1 if (cfg == "cfg-b" and run == 1) else 0
                (run_dir / "test.xml").write_text(
                    f'<testsuite errors="0" failures="{failures}" tests="1">'
                    f'<testcase time="3.5"/></testsuite>', encoding="utf-8")
                (run_dir / "job").symlink_to(job)
            unit = store.record_unit(
                batch_id=bid, paramset_id=cfg, config_name=cfg,
                params={"speed": speed, "map_file": "files/map.yaml"},
                objectives={}, measures={}, status="evaluated", result_dir=cfg)
            store.record_runs(unit, [read_run_outcome(tmp_path / cfg / str(r), tmp_path)
                                     for r in (0, 1)])
    return tmp_path


@pytest.fixture
def search_campaign(tmp_path):
    """A search over two rounds, the second of which also failed to compose a draw.

    One run per configuration keeps it small; what matters is that the units sit in
    *different* batches while their result dirs stay flat under the campaign root, which is
    why the batch cannot be recovered from a path.
    """
    with CampaignStore(tmp_path / STORE_FILENAME) as store:
        cid = store.create_campaign("s", {"search": {"per_batch": 1}}, mode="search",
                                    config_dir="_config")
        for idx, (cfg, objective) in enumerate((("c-early", 0.25), ("c-late", 0.75))):
            bid = store.open_batch(cid, idx, ".")
            run_dir = tmp_path / cfg / "0"
            run_dir.mkdir(parents=True)
            (run_dir / "test.xml").write_text(
                '<testsuite errors="0" failures="0" tests="1"><testcase time="1.0"/>'
                '</testsuite>', encoding="utf-8")
            unit = store.record_unit(
                batch_id=bid, paramset_id=cfg, config_name=cfg, params={"speed": objective},
                objectives={"score": objective}, measures={}, status="evaluated",
                result_dir=cfg)
            store.record_runs(unit, [read_run_outcome(run_dir, tmp_path)])
            if idx == 1:
                # A draw whose configuration could not be built: no config dir, no runs.
                store.record_unit(
                    batch_id=bid, paramset_id="c-dead", config_name="c-dead", params={},
                    objectives={}, measures={}, status="composition_failed", result_dir="")
    return tmp_path


def _rows(campaign_dir, sql):
    return query_data_db(campaign_dir, sql)["rows"]


# The replacement for each deleted tool, exactly as the descriptions state it.
_REPLACEMENTS = {
    "get_run_details":
        "SELECT status, passed, duration_s FROM run_view "
        "WHERE config_name='cfg-b' AND run_id=1",
    "get_run_sysinfo":
        "SELECT sysinfo_json FROM run_view WHERE config_name='cfg-a' AND run_id=0",
    "get_configuration_summary":
        "SELECT run_id, status, duration_s FROM run_view "
        "WHERE config_name='cfg-a' ORDER BY run_id",
    "get_configuration_scenario_parameter":
        "SELECT DISTINCT params_json FROM run_view WHERE config_name='cfg-b'",
    "get_campaign_summary (counts)":
        "SELECT config_name, status, COUNT(*) AS n FROM run_view GROUP BY 1, 2",
    "get_campaign_execution_details":
        "SELECT robovast_version, execution_type, image, image_revision, "
        "execution_started_at FROM campaign.campaign LIMIT 1",
    "config exploration (config_view)":
        "SELECT fullkey, type, value FROM config_view WHERE fullkey LIKE '$.execution%'",
}


@pytest.mark.parametrize("tool,sql", sorted(_REPLACEMENTS.items()))
def test_documented_replacement_query_runs(campaign, tool, sql):
    """Every documented replacement executes and returns rows (never an error)."""
    result = query_data_db(campaign, sql)
    assert result["columns"], f"{tool}: query returned no columns"
    assert result["rows"], f"{tool}: query returned no rows"


def test_run_view_does_not_bleed_across_configs(campaign):
    """``run_id`` is unique only WITHIN a config, so this is the whole point of the view.

    Both configs have a run 0; filtering on config_name AND run_id must return exactly
    one row. A hand-written query against ``campaign.run`` that forgot the ``unit`` join
    would return two and silently average across configurations.
    """
    rows = _rows(campaign, "SELECT config_name, run_id FROM run_view "
                           "WHERE config_name='cfg-a' AND run_id=0")
    assert len(rows) == 1
    assert rows[0]["config_name"] == "cfg-a"
    # The bleed this prevents: run_id alone matches a run in every config.
    assert len(_rows(campaign, "SELECT config_name FROM run_view WHERE run_id=0")) == 2


def test_run_view_carries_the_shared_host_record(campaign):
    """Both configs' runs ran in one job, so each row must resolve the same sysinfo."""
    rows = _rows(campaign, "SELECT config_name, run_id, job_dir, "
                           "json_extract(sysinfo_json,'$.cpu_name') AS cpu "
                           "FROM run_view ORDER BY config_name, run_id")
    assert len(rows) == 4
    assert {r["cpu"] for r in rows} == {"Intel Xeon"}
    assert {r["job_dir"] for r in rows} == {"_jobs/batch-0/job-0"}


def test_config_view_never_returns_a_truncated_config(campaign):
    """The property the ``atom`` column buys.

    ``SELECT config_json`` on this campaign exceeds the per-cell cap and comes back
    truncated. ``config_view`` must not reproduce that at any level: container rows carry
    NULL, so no row can hold a clipped subtree. A later "simplification" of the view to
    ``t.value`` instead of ``t.atom`` would break exactly this.
    """
    truncated = _rows(campaign, "SELECT config_json FROM campaign.campaign")
    assert "truncated" in truncated[0]["config_json"], \
        "fixture is too small to exercise the cap"

    rows = _rows(campaign, "SELECT fullkey, type, value FROM config_view", )
    assert rows
    for r in rows:
        if r["type"] in ("object", "array"):
            assert r["value"] is None, f"{r['fullkey']}: container rows must not carry a value"
        if isinstance(r["value"], str):
            assert "truncated" not in r["value"], f"{r['fullkey']} came back truncated"

    # And it is genuinely usable for the thing it replaces: reading the config.
    execution = {r["fullkey"]: r["value"] for r in _rows(
        campaign, "SELECT fullkey, value FROM config_view WHERE fullkey LIKE '$.execution%'")}
    assert execution["$.execution.containers.scenario.image"] == "sim:1"
    assert execution["$.execution.runs"] == 2


def test_describe_documents_the_views_it_expects_callers_to_use(campaign):
    """A view with no description is a table name an LLM has to guess the meaning of."""
    described = describe_data_db(campaign)
    by_name = {(t["schema"], t["table"]): t for t in described["tables"]}
    for name in ("run_view", "config_view"):
        entry = by_name[("temp", name)]
        assert entry.get("description"), f"{name} must carry a description"
        assert entry["columns"], f"{name} must report its columns"
    # The views come first: a schema dump is read top-down and they are the entry point.
    assert [t["table"] for t in described["tables"][:2]] == ["run_view", "config_view"]
    assert "run_view" in described["note"]


def test_run_view_works_before_postprocessing(campaign):
    """The premise of the whole collapse: no data.db yet, and the answers are still there."""
    assert not (campaign / "_execution" / "data.db").exists()
    rows = _rows(campaign, "SELECT status, COUNT(*) AS n FROM run_view GROUP BY status")
    assert {r["status"]: r["n"] for r in rows} == {"passed": 3, "failed": 1}


def test_run_view_reports_the_batch_of_a_batch_mode_campaign(campaign):
    """A batch campaign has exactly one round, so every row reports batch 0.

    Not a formality: the web tree decides whether to *group* by batch from
    ``campaign.mode``, and reads this column to do it. A NULL here would silently
    un-group a campaign that has a perfectly good round recorded.
    """
    rows = _rows(campaign, "SELECT config_name, run_id, batch FROM run_view")
    assert {r["batch"] for r in rows} == {0}


def test_run_view_separates_the_rounds_of_a_search(search_campaign):
    """Each configuration reports the round that proposed it — including one that never ran.

    The uncomposable draw is the interesting case: it has no ``run`` rows at all and so
    reaches the view through its second UNION arm. If that arm dropped ``batch``, a round
    whose every draw failed to compose would vanish from the search's history.
    """
    rows = _rows(search_campaign,
                 "SELECT config_name, run_id, batch, objective FROM run_view "
                 "ORDER BY batch, config_name, run_id")
    by_config = {r["config_name"]: r["batch"] for r in rows}
    assert by_config == {"c-early": 0, "c-late": 1, "c-dead": 1}

    # The documented per-round aggregate, which is what makes the view a search history.
    per_round = _rows(search_campaign,
                      "SELECT batch, COUNT(*) AS n FROM run_view GROUP BY 1 ORDER BY 1")
    assert [(r["batch"], r["n"]) for r in per_round] == [(0, 1), (1, 2)]

    dead = next(r for r in rows if r["config_name"] == "c-dead")
    assert dead["run_id"] is None and dead["batch"] == 1


def test_run_view_degrades_to_a_null_batch_on_a_store_without_the_batch_table(campaign):
    """Same column-parity rule as the host columns: shape first, values second."""
    conn = sqlite3.connect(campaign / STORE_FILENAME)
    # The FK is unenforced here, so the units survive their batch row -- which is exactly
    # the "recorded runs, unrecorded round" case the LEFT JOIN exists to keep visible.
    conn.executescript("DROP TABLE batch;")
    conn.commit()
    conn.close()

    result = query_data_db(campaign, "SELECT config_name, run_id, batch FROM run_view "
                                     "ORDER BY config_name, run_id")
    assert list(result["columns"]) == ["config_name", "run_id", "batch"]
    # Every run still listed: dropping them would lose results over missing metadata.
    assert len(result["rows"]) == 4
    assert all(r["batch"] is None for r in result["rows"])


def test_run_view_degrades_to_null_host_columns_on_an_old_store(campaign):
    """A store predating the ``job`` table keeps the SAME columns, with NULL host fields.

    Dropping the columns instead would give the view two different shapes depending on
    the store's age, so a caller's query would break rather than report "not known".
    """
    conn = sqlite3.connect(campaign / STORE_FILENAME)
    conn.executescript("DROP TABLE job; ALTER TABLE run DROP COLUMN job_id;")
    conn.commit()
    conn.close()

    result = query_data_db(campaign, "SELECT config_name, run_id, status, job_dir, "
                                     "sysinfo_json FROM run_view ORDER BY config_name")
    assert [c for c in result["columns"]] == [
        "config_name", "run_id", "status", "job_dir", "sysinfo_json"]
    assert len(result["rows"]) == 4
    assert all(r["job_dir"] is None and r["sysinfo_json"] is None for r in result["rows"])


def test_campaign_summary_counts_a_kill_apart_from_the_failures(monkeypatch):
    """``num_killed`` never lands in ``num_failed``, and a kill never makes a config "worst".

    A run an operator stopped says nothing about the system under test. Counting it as a
    failure would put a human decision into the campaign's measured outcome, and letting it
    rank a config into ``worst_configs`` would send the next reader to investigate a defect
    that does not exist.
    """
    from robovast.mcp_server import data_access
    from robovast.mcp_server.plugins import results

    per_config = [
        {"config_name": "cfg-a", "num_runs": 4, "success": 3, "failed": 1,
         "unknown": 0, "killed": 0},
        # Nothing wrong with cfg-b: someone stopped two of its runs by hand.
        {"config_name": "cfg-b", "num_runs": 4, "success": 2, "failed": 0,
         "unknown": 0, "killed": 2},
    ]
    monkeypatch.setattr(data_access, "rows",
                        lambda cid, sql: per_config if "GROUP BY config_name" in sql else [])
    monkeypatch.setattr("robovast.results_processing.advice.campaign_advice",
                        lambda _rows: {})

    summary = results.get_campaign_summary("campaign-x")

    assert summary["num_failed"] == 1, "a kill must not be counted as a failure"
    assert summary["num_killed"] == 2
    assert summary["worst_configs"][0]["name"] == "cfg-a", \
        "the config with the real failure must rank above the one that was merely stopped"


def test_campaign_summary_omits_num_killed_when_nothing_was_killed(monkeypatch):
    """A campaign nobody intervened in carries no key saying so."""
    from robovast.mcp_server import data_access
    from robovast.mcp_server.plugins import results

    monkeypatch.setattr(
        data_access, "rows",
        lambda cid, sql: ([{"config_name": "cfg-a", "num_runs": 2, "success": 2,
                            "failed": 0, "unknown": 0, "killed": 0}]
                          if "GROUP BY config_name" in sql else []))
    monkeypatch.setattr("robovast.results_processing.advice.campaign_advice",
                        lambda _rows: {})

    assert "num_killed" not in results.get_campaign_summary("campaign-x")
