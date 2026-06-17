# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Quadrotor search extract — selects the values the search optimizes.

Referenced from the ``.vast`` as ``./search/extract.py:QuadExtract``. Reads the
per-run ``metrics.csv`` produced by the ``QuadMetrics`` postprocessing plugin
(``./search/metrics.py``) plus ``test.xml`` for pass/fail, and aggregates over a
config's runs into the search **objective** (``failure_rate``) and **measures**
(the mean of the metric columns). Metric *computation* lives in QuadMetrics; this
just reads, aggregates, and names — so search and the analysis notebooks share
one source of truth (``metrics.csv``).

Parameterizable from the ``.vast`` (``extract.params``):
    metrics   per-run CSV filename to read (default ``metrics.csv``)
"""

import csv
from pathlib import Path

from robovast.common.campaign_data import read_test_result
from robovast.search.extractor import (Extractor, ExtractResult,
                                       completed_run_dirs)


def _metrics_row(run_dir: Path, metrics_file: str) -> dict | None:
    path = run_dir / metrics_file
    if not path.exists():
        return None
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return None
    return {k: float(v) for k, v in rows[0].items()}


class QuadExtract(Extractor):
    def extract(self, config_dir: Path) -> ExtractResult:
        metrics_file = self.params.get("metrics", "metrics.csv")
        runs = completed_run_dirs(config_dir)

        failures = sum(1 for r in runs if not read_test_result(r)["success"])
        failure_rate = failures / len(runs) if runs else 0.0

        per_run = [m for m in (_metrics_row(r, metrics_file) for r in runs) if m]
        if per_run:
            keys = per_run[0].keys()
            measures = {k: sum(m[k] for m in per_run) / len(per_run) for k in keys}
        else:
            measures = {"max_tilt": 0.0, "drift_dist": 0.0,
                        "landing_speed": 0.0, "control_effort": 0.0}
        return ExtractResult(objectives={"failure_rate": failure_rate}, measures=measures)
