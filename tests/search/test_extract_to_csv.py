# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""The extract_to_csv postprocessing adapter (reuses a search Extractor)."""

import csv

from robovast.results_processing.extract_plugins import ExtractToCsv


def _make_config(campaign_dir, name, outcomes):
    for run, failures in outcomes:
        d = campaign_dir / name / run
        d.mkdir(parents=True)
        (d / "test.xml").write_text(
            f'<testsuite errors="0" failures="{failures}" tests="1">'
            f'<testcase name="t" time="1.0"/></testsuite>')


def test_extract_to_csv_writes_per_config_metrics(tmp_path):
    campaign = tmp_path / "campaign-x"
    campaign.mkdir()
    _make_config(campaign, "ca", [("0", 1), ("1", 0)])   # failure_rate 0.5
    _make_config(campaign, "cb", [("0", 0), ("1", 0)])   # failure_rate 0.0
    (campaign / "_transient").mkdir()                    # reserved dir is skipped

    ok, msg = ExtractToCsv()(str(campaign), str(tmp_path), plugin="failure_rate",
                             file="metrics.csv")
    assert ok, msg

    def read(name):
        with open(campaign / name / "metrics.csv") as f:
            return list(csv.DictReader(f))[0]

    assert float(read("ca")["failure_rate"]) == 0.5
    assert float(read("cb")["failure_rate"]) == 0.0
    assert not (campaign / "_transient" / "metrics.csv").exists()
