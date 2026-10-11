# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The live and the read-only run-tally readers answer the same store with the same dict."""

from robovast.common.store import STORE_FILENAME, CampaignStore, read_run_counts

STATUSES = ("passed", "failed", "error", "killed", "invalid", "unknown")


def _run(run_id, status):
    return {"run_id": run_id, "status": status, "passed": int(status == "passed"),
            "errors": int(status == "error"), "failures": int(status == "failed"),
            "tests": 1, "duration_s": None, "start_time": None, "failure_message": None}


def test_both_readers_report_every_status(tmp_path):
    with CampaignStore(tmp_path / STORE_FILENAME) as store:
        campaign = store.create_campaign("c", {}, mode="batch")
        batch = store.open_batch(campaign, 0, ".")
        unit = store.record_unit(batch, "cfg", "cfg", {}, {}, {}, "mixed", "cfg")
        store.record_runs(unit, [_run(i, s) for i, s in enumerate(STATUSES)])
        live = store.run_counts(campaign)

    assert read_run_counts(tmp_path) == live
    assert live["num_invalid"] == 1
