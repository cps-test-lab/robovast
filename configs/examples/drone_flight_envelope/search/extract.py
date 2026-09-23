# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Search extract: what the drone campaign optimises and how it is described.

Referenced from a ``.vast`` as ``search/extract.py:EnvelopeExtract``. Reads the per-run
``metrics.csv`` that ``search/metrics.py:EnvelopeMetrics`` wrote, plus ``test.xml`` for pass/fail,
and aggregates a configuration's runs into one **objective** and a set of **measures**. The metric
*computation* lives in EnvelopeMetrics; this only reads, aggregates and names, so the search and the
notebooks share one source of truth.

The objective is ``envelope_failure`` -- the fraction of runs that did not hold the commanded
altitude. That is deliberately not the harness's own pass/fail: a run in which the drone never left
the pad is a perfectly successful *execution* of the trial, and the whole campaign is about finding
where that happens. Optimising the harness's success flag would search for broken infrastructure
instead.
"""

from __future__ import annotations

import csv
from pathlib import Path

from robovast.search.extractor import (Extractor, ExtractResult,
                                       completed_run_dirs)

#: Outcomes counted as being outside the flight envelope. `sagged` is included: a drone that cannot
#: hold the altitude it was commanded has run out of margin, even though it is still airborne.
FAILED_OUTCOMES = {"could_not_hover", "sagged"}

#: The measures a QD archive is illuminated over. Named here rather than taken from whatever columns
#: happen to be in the CSV, so adding a diagnostic column cannot silently redefine the archive.
MEASURE_COLUMNS = ("max_tilt_deg", "tracking_rmse", "mean_altitude_error", "landing_error")


def _metrics_row(run_dir: Path, metrics_file: str) -> dict | None:
    path = run_dir / metrics_file
    if not path.exists():
        return None
    with open(path, newline="") as handle:
        rows = list(csv.DictReader(handle))
    return rows[0] if rows else None


class EnvelopeExtract(Extractor):
    def extract(self, config_dir: Path) -> ExtractResult:
        metrics_file = self.params.get("metrics", "metrics.csv")
        runs = completed_run_dirs(config_dir)
        rows = [r for r in (_metrics_row(r, metrics_file) for r in runs) if r]

        if not rows:
            # No data is not the same as no failures. Reporting 0.0 would tell the search this
            # corner of the space is safe, and it would stop looking exactly where the runs broke.
            return ExtractResult(
                objectives={"envelope_failure": 1.0},
                measures=dict.fromkeys(MEASURE_COLUMNS, 0.0),
            )

        outside = sum(1 for r in rows if r.get("outcome") in FAILED_OUTCOMES)
        measures = {}
        for column in MEASURE_COLUMNS:
            values = [float(r[column]) for r in rows if r.get(column) not in (None, "")]
            measures[column] = sum(values) / len(values) if values else 0.0

        return ExtractResult(
            objectives={"envelope_failure": outside / len(rows)},
            measures=measures,
        )
