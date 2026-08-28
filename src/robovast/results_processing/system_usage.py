# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""Per-run slices of the CONTAINER-level counters, beside the per-process ones.

The sibling of :mod:`robovast.results_processing.resource_usage`, and separate from it for a
reason that is structural rather than tidy: ``resource_usage`` is per **process** by contract,
and every reader aggregates it that way -- ``advice.USAGE_SQL`` sums a tick's rows to get the
container's cores, and the web UI lists them as processes. A container-level figure written
into it as a synthetic process row would be summed as though it were one, and would appear in
the UI as a process nobody can find. So it gets its own table.

**Column-generic, deliberately.** This module names no metric. The sampler decides what a
container can report (``monitor_resources.PROBES``), writes it as a header, and everything
downstream carries it through: here by passing every non-key column verbatim, and in the
CSV -> ``data.db`` ingest because that step already types columns from what it finds. Adding a
counter is therefore a change to the sampler and to nothing else -- which is the property
``resource_usage`` does not have, where the column list is repeated in five places.
"""

from __future__ import annotations

import csv
import os
from typing import Dict, List, Optional, Sequence, Tuple

from robovast.common.log_tail import MAIN_CONTAINER

from . import run_slices

#: The per-run table this module writes, beside ``resource_usage.csv``.
FILENAME = "system_usage.csv"

#: What the sampler writes per job, one file per container.
CSV_PREFIX = "system_usage_"

#: The wall stamp every sampler row carries. It is the join key to a run, and it is the one
#: column this module *does* know about -- everything else is passed through untouched.
WALL_COLUMN = "timestamp"

#: Written ahead of the metric columns, in this order. ``timestamp`` is SIM time here, matching
#: ``resource_usage.csv``, so the two tables join on the same clock; the sampler's own wall
#: stamp survives as ``wall_ts``.
KEY_FIELDS = ("timestamp", "wall_ts", "in_window", "container")

#: One sample: which container, when (wall epoch), and whatever it reported.
Sample = Tuple[str, float, Dict[str, str]]


def fieldnames(columns: Sequence[str]) -> List[str]:
    """The output header: the keys, then the metric columns in a stable order."""
    return list(KEY_FIELDS) + list(columns)


def read_container_csv(path: str, container: str) -> Tuple[List[str], List[Sample]]:
    """``(metric columns, samples)`` from one container's file.

    A row whose wall stamp will not parse is dropped rather than guessed at: without it the
    row cannot be attributed to a run, and attributing it to the wrong one is worse than
    losing it.
    """
    try:
        with open(path, newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            columns = [c for c in (reader.fieldnames or []) if c != WALL_COLUMN]
            samples: List[Sample] = []
            for row in reader:
                try:
                    wall = float(row.get(WALL_COLUMN) or "")
                except (TypeError, ValueError):
                    continue
                samples.append((container, wall, {c: (row.get(c) or "") for c in columns}))
    except OSError:
        return [], []
    return columns, samples


def collect_job_rows(job_dir: str) -> Tuple[List[str], List[Sample]]:
    """Every container's samples for one job, and the union of the columns they reported.

    The union matters because probe availability is a property of the *container image*, not
    of the job: a CPU-only sidecar and a simulator can legitimately report different sets, and
    the run's table has to hold both with blanks where one had nothing to say.
    """
    if not os.path.isdir(job_dir):
        return [], []
    columns: List[str] = []
    samples: List[Sample] = []
    for name in sorted(os.listdir(job_dir)):
        if not (name.startswith(CSV_PREFIX) and name.endswith(".csv")):
            continue
        container = run_slices.container_of(name) or MAIN_CONTAINER
        found, rows = read_container_csv(os.path.join(job_dir, name), container)
        for column in found:
            if column not in columns:
                columns.append(column)
        samples.extend(rows)
    return sorted(columns), samples


def rows_for_slice(columns: Sequence[str], samples: Sequence[Sample],
                   slice_: run_slices.RunSlice) -> List[dict]:
    """The rows one run claims, on its own clock.

    Same partition and the same clock as ``resource_usage``: a job serves several runs, and a
    counter copied into all of them would report a multiple of the truth in every aggregate.
    ``timestamp`` is left empty where the clock map cannot answer -- before the simulator
    published ``/clock`` and after it stopped -- rather than extrapolated to zero.
    """
    rows: List[dict] = []
    for container, wall, values in samples:
        if not slice_.claims(wall):
            continue
        sim: Optional[float] = slice_.clock.to_sim(wall) if slice_.clock else None
        row = {
            "timestamp": "" if sim is None else f"{sim:.6f}",
            "wall_ts": f"{wall:.9f}",
            "in_window": slice_.in_window(wall),
            "container": container,
        }
        row.update({column: values.get(column, "") for column in columns})
        rows.append(row)
    return rows
