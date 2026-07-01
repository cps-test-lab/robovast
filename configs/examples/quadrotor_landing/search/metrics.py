# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Quadrotor metrics postprocessing plugin.

Per *run*, derives behaviour metrics from the raw ``trajectory.csv`` the sim
writes and stores them next to it as ``metrics.csv``. This is the single place
the SUT-specific metric computation lives; it feeds both the analysis notebooks
(``analysis_run.ipynb``) and the search (the extractor reads ``metrics.csv``).

Used as a local-file postprocessing plugin (``./search/metrics.py:QuadMetrics``)
in either ``results_processing.postprocessing`` (batch) or ``search.postprocessing``
(search). Run discovery is depth-agnostic (globs for ``trajectory.csv``), so it
works whether pointed at the parent results dir or a single campaign root.
"""

import csv
from pathlib import Path
from typing import Tuple

from robovast.results_processing.postprocessing_plugins import \
    BasePostprocessingPlugin


def _metrics_for_run(run_dir: Path, trajectory: str) -> dict | None:
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
    effort = sum(tilts[i] * (ts[i] - ts[i - 1]) for i in range(1, len(ts)))
    return {
        "max_tilt": max(tilts),
        "drift_dist": abs(xs[-1]),
        "landing_speed": abs(vzs[-1]),
        "control_effort": effort,
    }


class QuadMetrics(BasePostprocessingPlugin):
    def __call__(self, results_dir: str, config_dir: str,
                 trajectory: str = "trajectory.csv", file: str = "metrics.csv",
                 force: bool = False, **kwargs) -> Tuple[bool, str]:
        written = skipped = 0
        for traj in sorted(Path(results_dir).rglob(trajectory)):
            out = traj.parent / file
            # Incremental: skip runs already processed (output newer than input),
            # so re-running over the whole campaign only writes new runs. ``force``
            # reprocesses everything.
            if not force and out.exists() and out.stat().st_mtime >= traj.stat().st_mtime:
                skipped += 1
                continue
            metrics = _metrics_for_run(traj.parent, trajectory)
            if metrics is None:
                continue
            with open(out, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(list(metrics.keys()))
                w.writerow([metrics[k] for k in metrics])
            written += 1
        suffix = f" ({skipped} up-to-date)" if skipped else ""
        return True, f"QuadMetrics wrote {file} for {written} run(s){suffix}"
