# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""The results GUI reads campaigns from the store (no filesystem-walk).

Exercises the two pure store-reading helpers on RunResultsAnalyzer without
constructing the Qt window (they use neither Qt nor instance state). Skipped
when PySide6 is not installed.
"""

import textwrap

import pytest

pytest.importorskip("PySide6")

from robovast.common.campaign_index import build_campaign_store  # noqa: E402
from robovast.common.store import STORE_FILENAME  # noqa: E402
from robovast.evaluation.result_analyzer.result_analyzer import \
    RunResultsAnalyzer as R  # noqa: E402

VAST = textwrap.dedent("""\
    version: 1
    configuration:
    - name: ca
      parameters:
      - speed: 1.0
    execution: {image: img, runs: 2, scenario_file: scenario.osc}
    evaluation:
      visualization:
      - View: {run: r.ipynb, config: c.ipynb}
    """)


def _batch_campaign(root):
    campaign = root / "campaign-2026-06-17-101010"
    (campaign / "_config").mkdir(parents=True)
    (campaign / "_config" / "q.vast").write_text(VAST)
    for run in range(2):
        rd = campaign / "ca" / str(run)
        rd.mkdir(parents=True)
        (rd / "test.xml").write_text(
            '<testsuite errors="0" failures="0" tests="1">'
            '<testcase name="t" time="1"/></testsuite>')
    build_campaign_store(campaign)
    return campaign


def test_read_campaign_store_and_workloads(tmp_path):
    campaign = _batch_campaign(tmp_path)
    # self=None: these helpers touch neither Qt nor instance state.
    entry = R._read_campaign_store(None, campaign, campaign / STORE_FILENAME)
    assert entry["mode"] == "batch"
    assert entry["config_file"].endswith("q.vast")
    assert len(entry["batches"]) == 1
    units = entry["batches"][0]["units"]
    assert [(u["config_name"], u["status"], u["n_samples"]) for u in units] == [("ca", "passed", 2)]

    workloads = R._build_workloads(
        None, entry["config_json"]["evaluation"], entry["config_dir"], "camp")
    assert len(workloads) == 1
    assert workloads[0].name == "View"
    assert workloads[0].run_nb.endswith("r.ipynb")
    assert workloads[0].config_nb.endswith("c.ipynb")
