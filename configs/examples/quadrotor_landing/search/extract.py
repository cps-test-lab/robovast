# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Quadrotor search extract — the one SUT-specific scoring module.

Referenced from the ``.vast`` as ``./search/extract.py:QuadExtract``. Derives,
per parameter set, the search **objective** and **measures** from the raw
``trajectory.csv`` the sim writes (and ``test.xml`` for pass/fail), aggregated
over the config's runs. No ``metrics.csv`` is produced by the sim — this is the
single place those values are computed, reused for both search and (via
``extract_to_csv``) the analysis notebooks.

Parameterizable from the ``.vast`` (``extract.params``):
    trajectory   filename to read (default ``trajectory.csv``)
"""

import csv
from pathlib import Path

from robovast.common.campaign_data import read_test_result
from robovast.search.extractor import Extractor, ExtractResult, completed_run_dirs


def _measures_for_run(run_dir: Path, trajectory: str) -> dict | None:
    path = run_dir / trajectory
    if not path.exists():
        return None
    ts, xs, vzs, tilts = [], [], [], []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            ts.append(float(row["t"]))
            xs.append(float(row["x"]))
            vzs.append(float(row["vz"]))
            tilts.append(abs(float(row["tilt"])))
    if not ts:
        return None
    # Integrate |tilt| over time for control effort.
    effort = sum(abs(tilts[i]) * (ts[i] - ts[i - 1]) for i in range(1, len(ts)))
    return {
        "max_tilt": max(tilts),
        "drift_dist": abs(xs[-1]),
        "landing_speed": abs(vzs[-1]),
        "control_effort": effort,
    }


class QuadExtract(Extractor):
    def extract(self, config_dir: Path) -> ExtractResult:
        trajectory = self.params.get("trajectory", "trajectory.csv")
        runs = completed_run_dirs(config_dir)

        failures = sum(1 for r in runs if not read_test_result(r)["success"])
        objective = failures / len(runs) if runs else 0.0

        per_run = [m for m in (_measures_for_run(r, trajectory) for r in runs) if m]
        if per_run:
            keys = per_run[0].keys()
            measures = {k: sum(m[k] for m in per_run) / len(per_run) for k in keys}
        else:
            measures = {"max_tilt": 0.0, "drift_dist": 0.0,
                        "landing_speed": 0.0, "control_effort": 0.0}
        return ExtractResult(objectives={"failure_rate": objective}, measures=measures)
