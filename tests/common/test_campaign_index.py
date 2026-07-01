# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Post-hoc batch indexer: build a campaign store from a results tree."""

import textwrap

from robovast.common.campaign_index import build_campaign_store
from robovast.common.store import STORE_FILENAME, CampaignStore

VAST = textwrap.dedent("""\
    version: 1
    configuration:
    - name: ca
      parameters:
      - speed: 1.0
    execution:
      image: img
      runs: 2
      scenario_file: scenario.osc
    evaluation:
      visualization:
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

    # config_json carries the evaluation.visualization block for the GUI.
    import json
    with CampaignStore(store_path) as store:
        cfg = json.loads(store.list_campaigns()[0]["config_json"])
    assert cfg["evaluation"]["visualization"][0]["Analysis"]["run"] == "analysis/run.ipynb"


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
