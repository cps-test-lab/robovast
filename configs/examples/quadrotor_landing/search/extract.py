# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Quadrotor search extract — selects the values the search optimizes.

Referenced from the ``.vast`` as ``./search/extract.py:QuadExtract``. Reads the
per-run ``metrics.csv`` produced by the ``QuadMetrics`` postprocessing plugin
(``./search/metrics.py``) plus ``test.xml`` for pass/fail, and aggregates over a
config's runs into the search **objective** (``failure_rate``) and **measures**
(the behaviour axes the QD archive bins on). Metric *computation* lives in
QuadMetrics; this just reads, aggregates, and names — so search and the analysis
notebooks share one source of truth (``metrics.csv``).

**Nothing here is invented.** A cell that produced no result, or produced results
this could not read, raises :class:`~robovast.search.extractor.NoSampleError`: the
framework records the cell and carries on, and no number is put where a measurement
should be. Both fabrications this once had were the ones the rest of RoboVAST
documents as forbidden, and both pointed the search the wrong way —

* ``failure_rate = 0.0`` for a cell with no runs. The objective is **maximized**, so
  0.0 is the least interesting score there is: a cell whose every run died of
  infrastructure looked like a cell where nothing failed, and the search steered
  away from exactly the region it was hunting.
* zero-valued measures for a cell with no metrics. Those are archive coordinates,
  so ``(0, 0, 0, 0)`` is a real cell — the calmest corner of the behaviour space —
  and an unmeasurable configuration became an elite the search then chased.

**Aggregated worst-case, not averaged**, for a reason measured on this campaign:
behaviour measures averaged over five runs filled 3 of 512 archive cells, because
averaging pulls every cell toward the middle of the behaviour space before the
archive ever sees it (see :mod:`robovast.search.aggregate`). Every measure here is
a quantity where *higher is worse* — tilt, drift, landing speed, control effort —
so the pessimistic end is the maximum.

Parameterizable from the ``.vast`` (``extract.params``):
    metrics    per-run CSV filename to read (default ``metrics.csv``)
    aggregate  ``worst`` (default), ``quantile`` or ``mean``
"""

import csv
import logging
from pathlib import Path

from robovast.common.campaign_data import read_test_result
from robovast.search.aggregate import aggregate
from robovast.search.extractor import (Extractor, ExtractResult, NoSampleError,
                                       completed_run_dirs)

logger = logging.getLogger(__name__)

#: The archive's behaviour axes. Every one is a cost — more tilt, more drift, a faster
#: touchdown, more control effort are all worse — so the pessimistic end of each is its
#: maximum, which is what ``higher_is_safer=False`` tells :func:`aggregate`.
MEASURES = ("max_tilt", "drift_dist", "landing_speed", "control_effort")


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
        how = self.params.get("aggregate", "worst")
        runs = completed_run_dirs(config_dir)

        if not runs:
            # Not 0.0. This cell produced no result at all, and for a MAXIMIZED failure
            # rate 0.0 is the least interesting score there is -- so the search would
            # steer away from the parameter sets whose runs are dying, which is where the
            # failures it hunts actually are. The built-in `failure_rate` extractor
            # refuses this for the same reason; so does this one.
            raise NoSampleError(
                f"{config_dir}: no run produced a result (no test.xml), so failure_rate "
                f"has nothing to measure -- scoring 0.0 would claim that nothing failed")

        failures = sum(1 for r in runs if not read_test_result(r)["success"])

        per_run = [m for m in (_metrics_row(r, metrics_file) for r in runs) if m]
        if not per_run:
            # Nor zeros. These are ARCHIVE COORDINATES, so (0, 0, 0, 0) is a real cell --
            # the calmest corner of the behaviour space -- and a configuration nothing
            # could be measured from would become an elite the search then chases.
            raise NoSampleError(
                f"{config_dir}: {len(runs)} run(s) produced a verdict but no "
                f"'{metrics_file}', so there is no behaviour to place in the archive. "
                f"QuadMetrics writes one per run from 'trajectory.csv' -- check that the "
                f"trajectory was recorded and that QuadMetrics ran for this batch.")
        if len(per_run) < len(runs):
            # `n_samples` counts completed runs, so it is larger than what this aggregated
            # over; that difference has to be visible rather than inferred.
            logger.warning(
                "%s: %d of %d run(s) produced no '%s' and were left out of the "
                "aggregation; the cell is scored over the remaining %d.",
                config_dir, len(runs) - len(per_run), len(runs), metrics_file,
                len(per_run))

        measures = {
            name: aggregate([m[name] for m in per_run if name in m],
                            how=how, higher_is_safer=False)
            for name in MEASURES if any(name in m for m in per_run)
        }
        return ExtractResult(
            objectives={"failure_rate": failures / len(runs)},
            measures=measures)
