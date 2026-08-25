# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Post-hoc batch indexer: build a campaign store from a results tree."""

import textwrap

from robovast.common.campaign_index import build_campaign_store
from robovast.common.store import (STORE_FILENAME, CampaignStore, read_campaign_created_at,
                                   read_campaign_description)

VAST = textwrap.dedent("""\
    version: 3
    configuration:
    - name: ca
      parameters:
      - speed: 1.0
    execution:
      containers: {scenario: {image: img}}
      runs: 2
      scenario_file: scenario.osc
    visualization:
      results:
        explorer:
          notebooks:
          - Analysis:
              run: analysis/run.ipynb
              campaign: analysis/camp.ipynb
    """)


def _test_xml(failures):
    return (f'<testsuite errors="0" failures="{failures}" tests="1">'
            f'<testcase name="t" time="1.0"/></testsuite>')


def _make_campaign(root, configs):
    """configs: {name: [failures_per_run...]} -> writes a batch campaign tree."""
    campaign = root / "campaign-2026-06-17-101010"
    (campaign / "_config").mkdir(parents=True)
    (campaign / "_config" / "quad.vast").write_text(VAST)
    for name, runs in configs.items():
        cfg_dir = campaign / name
        (cfg_dir / "_config").mkdir(parents=True)
        (cfg_dir / "_config" / "scenario.config").write_text(
            f"test_scenario:\n  config_name: {name}\n")
        for i, failures in enumerate(runs):
            run_dir = cfg_dir / str(i)
            run_dir.mkdir(parents=True)
            (run_dir / "test.xml").write_text(_test_xml(failures))
    return campaign


def test_build_campaign_store_batch(tmp_path):
    campaign = _make_campaign(tmp_path, {
        "ca": [0, 0],   # all pass
        "cb": [1, 1],   # all fail
        "cc": [0, 1],   # mixed
    })

    store_path = build_campaign_store(campaign)
    assert store_path == campaign / STORE_FILENAME
    assert store_path.exists()

    with CampaignStore(store_path) as store:
        campaigns = store.list_campaigns()
        assert len(campaigns) == 1
        row = campaigns[0]
        assert row["mode"] == "batch"
        assert row["config_dir"] == "_config"

        batches = store.batches(row["id"])
        assert len(batches) == 1 and batches[0]["idx"] == 0

        units = {u["config_name"]: u for u in store.units(batches[0]["id"])}
        assert set(units) == {"ca", "cb", "cc"}
        assert units["ca"]["status"] == "passed"
        assert units["cb"]["status"] == "failed"
        assert units["cc"]["status"] == "mixed"
        assert all(u["n_samples"] == 2 for u in units.values())
        assert units["ca"]["result_dir"] == "ca"

    # config_json carries the explorer notebooks, which is how they resolve for a
    # campaign read back from its store.
    import json
    with CampaignStore(store_path) as store:
        cfg = json.loads(store.list_campaigns()[0]["config_json"])
    notebooks = cfg["visualization"]["results"]["explorer"]["notebooks"]
    assert notebooks[0]["Analysis"]["run"] == "analysis/run.ipynb"


def _write_execution_record(campaign, execution_time: str) -> None:
    """Write the ``_execution/execution.yaml`` the local run script produces."""
    (campaign / "_execution").mkdir(parents=True, exist_ok=True)
    (campaign / "_execution" / "execution.yaml").write_text(
        f"execution_time: '{execution_time}'\nruns: 1\nexecution_type: local\n")


def test_created_at_is_the_recorded_start_not_the_indexing_time(tmp_path):
    """created_at means "campaign start" — even though this indexer runs afterwards.

    The store cannot be written live for local batch runs, so this indexer builds it
    after the fact. Stamping ``time.time()`` there would record the *indexing* time,
    which the service then shows as ``started_at`` and orders the campaign list by —
    putting an old campaign among the most recent ones.
    """
    campaign = _make_campaign(tmp_path, {"ca": [0]})
    _write_execution_record(campaign, "2026-06-17T10:10:10Z")

    build_campaign_store(campaign)
    assert read_campaign_created_at(campaign) == "2026-06-17T10:10:10+00:00"

    # A rebuild must not restamp it: re-indexing an old campaign must not move it.
    build_campaign_store(campaign, force=True)
    assert read_campaign_created_at(campaign) == "2026-06-17T10:10:10+00:00"


def test_rebuild_keeps_the_description(tmp_path):
    """The launcher's description is the one column no results tree can supply, so a
    forced rebuild must carry it over instead of blanking it."""
    campaign = _make_campaign(tmp_path, {"ca": [0]})
    _write_execution_record(campaign, "2026-06-17T10:10:10Z")
    build_campaign_store(campaign)
    with CampaignStore(campaign / STORE_FILENAME) as store:
        store._conn.execute("UPDATE campaign SET description = ?", ("the full sweep",))
        store._conn.commit()

    build_campaign_store(campaign, force=True)
    assert read_campaign_description(campaign) == "the full sweep"


def test_created_at_is_null_when_the_start_was_never_recorded(tmp_path):
    """No execution record -> unknown start time, not a guessed one."""
    campaign = _make_campaign(tmp_path, {"ca": [0]})  # no _execution/execution.yaml

    build_campaign_store(campaign)
    assert read_campaign_created_at(campaign) is None


def test_live_created_at_defaults_to_now(tmp_path):
    """The live path is unchanged: omitting created_at stamps the current time.

    This is what the controller does at the start of a run, in both modes.
    """
    import time

    before = time.time()
    with CampaignStore(tmp_path / "search" / STORE_FILENAME) as store:
        store.create_campaign("search-2026-06-17-101010", {}, mode="search")
        created_at = store.list_campaigns()[0]["created_at"]
    assert before <= created_at <= time.time()


def test_build_campaign_store_idempotent_and_force(tmp_path):
    campaign = _make_campaign(tmp_path, {"ca": [0]})
    first = build_campaign_store(campaign)
    mtime = first.stat().st_mtime

    # No change to the tree -> store left untouched.
    again = build_campaign_store(campaign)
    assert again.stat().st_mtime == mtime

    # force rebuilds.
    forced = build_campaign_store(campaign, force=True)
    assert forced.exists()
