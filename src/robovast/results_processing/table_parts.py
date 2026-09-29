# Copyright (C) 2026 Frederik Pasch
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions
# and limitations under the License.
#
# SPDX-License-Identifier: Apache-2.0

"""A campaign's tables built in parts: which runs a part holds, what it stages, how parts merge.

Building a large campaign's tables is decoding, and decoding is per run, so the work splits
into parts that build independently -- each in its own pod on a cluster -- and one merge
that needs no decoding at all. This module is the part of that which needs no cluster:

* **Planning** (:func:`plan_parts`): the unit is a scenario job with its runs, as the decoder
  sees them (:func:`robovast_decode.build.find_runs`), because a job's records are read as its
  one run's; units are packed in run order into parts of at most *runs_per_part* runs, so a
  part holds whole configurations where it can and its compacted file compresses as one.
* **Splitting** (:func:`split_part`): a part that could not be built -- a pod that ran out of
  memory -- is split into two halves of its units; a part of one unit cannot be split.
* **Membership** (:func:`write_part`, :func:`read_part`): what the service planned, under
  :data:`TABLE_PARTS_DIR`, which only the service writes; a pod names its part and the
  service stages exactly those runs (:func:`part_skip`).
* **Building** (:func:`build_part`): a staged part is a campaign directory with only its runs;
  its tables are built and compacted there, and what it delivers is its cache under
  :func:`part_cache_rel`.
* **Merging** (:func:`merge_parts`): each table's part files become one compacted file, the run
  entries enter the manifest, and the parts are removed (:func:`robovast_decode.compact.merge`).
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from robovast_decode.build import find_runs
from robovast_decode.tables import CACHE_DIR, MANIFEST, TABLES_DIR

#: Where the service keeps the parts it planned, relative to the campaign directory.
TABLE_PARTS_DIR = "_execution/table_parts"

#: Where a part delivers its tables, relative to the campaign's cache.
PARTS_CACHE_DIR = "parts"

#: A part's name: what a pod asks for, so nothing it could not be.
_PART_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


@dataclass
class Part:
    """One part: the runs it builds (``config/run``) and the job directories they ran in
    (campaign-relative, ``_jobs/...``)."""
    name: str
    runs: List[str] = field(default_factory=list)
    jobs: List[str] = field(default_factory=list)


def plan_units(campaign_dir: str) -> List[Part]:
    """The campaign's units, in run order: a job with its runs, or a run without a job."""
    units: Dict[str, Part] = {}
    for run in sorted(find_runs(campaign_dir), key=lambda r: (r.config_name, r.run_id)):
        job = (os.path.relpath(run.job_dir, campaign_dir).replace(os.sep, "/")
               if run.job_dir else "")
        unit = units.setdefault(job or f"run:{run.key}", Part(name=""))
        unit.runs.append(run.key)
        if job and job not in unit.jobs:
            unit.jobs.append(job)
    return list(units.values())


def plan_parts(campaign_dir: str, runs_per_part: int, prefix: str = "part") -> List[Part]:
    """The campaign's runs as parts of at most *runs_per_part* runs, in run order.

    A unit larger than the budget is a part of its own: a job's runs are never divided.
    """
    if runs_per_part < 1:
        raise ValueError(f"a part holds at least one run, not {runs_per_part}")
    parts: List[Part] = []
    current = Part(name="")
    for unit in plan_units(campaign_dir):
        if current.runs and len(current.runs) + len(unit.runs) > runs_per_part:
            parts.append(current)
            current = Part(name="")
        current.runs += unit.runs
        current.jobs += unit.jobs
    if current.runs:
        parts.append(current)
    for i, part in enumerate(parts, 1):
        part.name = f"{prefix}-{i}"
    return parts


def split_part(campaign_dir: str, part: Part) -> List[Part]:
    """*part* as two parts of about half its runs each, by whole units.

    Raises ``ValueError`` for a part of one unit: nothing smaller can be built.
    """
    wanted = set(part.runs)
    units = [u for u in plan_units(campaign_dir) if wanted.intersection(u.runs)]
    if len(units) < 2:
        raise ValueError(f"part {part.name} holds one unit ({', '.join(part.runs)}), which "
                         "cannot be split")
    half = len(part.runs) / 2
    first, second = [], []
    for unit in units:
        (first if sum(len(u.runs) for u in first) < half else second).append(unit)
    if not second:
        second.append(first.pop())
    return [Part(name=f"{part.name}{suffix}", runs=[r for u in group for r in u.runs],
                 jobs=[j for u in group for j in u.jobs])
            for suffix, group in (("a", first), ("b", second))]


def _part_path(campaign_dir: str, name: str) -> str:
    if not _PART_NAME.match(name):
        raise KeyError(f"{name!r} is not a table part's name")
    return os.path.join(campaign_dir, TABLE_PARTS_DIR, f"{name}.json")


def write_part(campaign_dir: str, part: Part) -> None:
    """Record *part*'s membership, for the pod that asks for it by name."""
    path = _part_path(campaign_dir, part.name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.incoming"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"runs": part.runs, "jobs": part.jobs}, fh, indent=1)
    os.replace(tmp, path)


def read_part(campaign_dir: str, name: str) -> Part:
    """The part the service planned under *name*; ``KeyError`` for one it did not."""
    try:
        with open(_part_path(campaign_dir, name), encoding="utf-8") as fh:
            members = json.load(fh)
    except FileNotFoundError as exc:
        raise KeyError(f"no table part {name!r} was planned") from exc
    return Part(name=name, runs=list(members.get("runs") or []),
                jobs=list(members.get("jobs") or []))


def clear_parts(campaign_dir: str) -> None:
    """Remove the planned membership and anything parts delivered."""
    shutil.rmtree(os.path.join(campaign_dir, TABLE_PARTS_DIR), ignore_errors=True)
    shutil.rmtree(os.path.join(campaign_dir, CACHE_DIR, PARTS_CACHE_DIR), ignore_errors=True)


def part_skip(campaign_dir: str, name: str) -> Callable[[str], bool]:
    """What staging part *name* leaves out, by campaign-relative path.

    Kept: the part's runs and the job directories they ran in, and what the decoder reads
    beside them -- the campaign's record, its ``_config``, ``_transient`` and ``_execution``
    (the decoder's configuration, the job links). Left out: every other run and job, the
    calibration probes, the planned parts, and every cache.
    """
    part = read_part(campaign_dir, name)
    runs = set(part.runs)
    jobs = [j.strip("/") + "/" for j in part.jobs]

    def skip(rel: str) -> bool:
        parts = rel.split("/")
        if any(p.startswith(".") for p in parts):
            return True
        if parts[0] == "_jobs":
            here = rel + "/"
            return not any(j.startswith(here) or here.startswith(j) for j in jobs)
        if parts[0] in ("_config", "_transient", "_execution"):
            return rel == TABLE_PARTS_DIR or rel.startswith(TABLE_PARTS_DIR + "/")
        if parts[0].startswith("_"):
            return True                                  # _calibration, _control, ...
        if len(parts) < 2 or not parts[1].isdigit():
            return False                                 # the record, a config's own files
        return f"{parts[0]}/{parts[1]}" not in runs

    return skip


def part_cache_rel(generation: int, name: str) -> str:
    """Where part *name* of build *generation* delivers its cache, relative to the campaign."""
    return f"{CACHE_DIR}/{PARTS_CACHE_DIR}/{generation}/{name}"


def build_part(campaign_dir: str, tables: Optional[Iterable[str]], workers: int,
               generation: int, name: str, out: str, progress=None) -> str:
    """Build and compact the tables of a staged part in *campaign_dir*, laid out under *out*.

    *campaign_dir* holds only the part's runs (:func:`part_skip`), so building every run
    there builds the part. *out* then holds the part's cache at :func:`part_cache_rel`, to be
    delivered into the campaign as it is; it is returned.
    """
    from robovast_data import Engine, Scope  # pylint: disable=import-outside-toplevel
    engine = Engine([Scope(campaign_dir)], workers=workers)
    built = engine.build(tables, progress=progress)
    for problem in built.problems:
        print(f"  {problem}", flush=True)
    target = os.path.join(out, part_cache_rel(generation, name))
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(target)
    cache = os.path.join(campaign_dir, CACHE_DIR)
    for entry in (MANIFEST, TABLES_DIR):
        if os.path.exists(os.path.join(cache, entry)):
            os.replace(os.path.join(cache, entry), os.path.join(target, entry))
    return out


def merge_parts(campaign_dir: str, parts: Iterable[Tuple[int, str]], *,
                workers: int = 1, progress=None):
    """Merge the delivered *parts* -- ``(generation, name)`` each -- into the campaign's
    tables; what compacting did. The parts' deliveries are removed.

    Every part must have delivered: a missing one raises ``FileNotFoundError`` naming it,
    rather than a merge that leaves its runs out.
    """
    from robovast_decode.compact import merge  # pylint: disable=import-outside-toplevel
    roots = []
    for generation, name in parts:
        rel = part_cache_rel(generation, name)
        if not os.path.isfile(os.path.join(campaign_dir, rel, MANIFEST)):
            raise FileNotFoundError(f"table part {name} delivered nothing to {rel}")
        roots.append(rel[len(CACHE_DIR) + 1:])
    report = merge(campaign_dir, roots, workers=workers, progress=progress)
    shutil.rmtree(os.path.join(campaign_dir, CACHE_DIR, PARTS_CACHE_DIR), ignore_errors=True)
    return report


def main(argv: Optional[List[str]] = None) -> int:
    """``python3 -m robovast.results_processing.table_parts``: what a table-building pod runs
    over the part it staged."""
    import argparse  # pylint: disable=import-outside-toplevel
    import sys  # pylint: disable=import-outside-toplevel
    parser = argparse.ArgumentParser(prog="robovast.results_processing.table_parts",
                                     description="Build and compact a staged part's tables.")
    parser.add_argument("campaign_dir")
    parser.add_argument("--part", required=True)
    parser.add_argument("--generation", type=int, required=True)
    parser.add_argument("--workers", type=int, required=True)
    parser.add_argument("--out", required=True, help="where the delivery is laid out")
    parser.add_argument("--table", action="append", dest="tables")
    args = parser.parse_args(argv)
    build_part(args.campaign_dir, args.tables, args.workers, args.generation, args.part,
               args.out)
    print(f"part {args.part}: built, delivering from {args.out}", file=sys.stderr)
    return 0


__all__ = ["PARTS_CACHE_DIR", "Part", "TABLE_PARTS_DIR", "build_part", "clear_parts",
           "merge_parts", "part_cache_rel", "part_skip", "plan_parts", "plan_units",
           "read_part", "split_part", "write_part"]


if __name__ == "__main__":
    raise SystemExit(main())
