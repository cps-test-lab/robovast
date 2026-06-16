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

"""Packing of scenario work items into jobs.

A *work item* is one configuration executed at one run number (one scenario
execution). A *packer* groups work items into :class:`JobSpec`s; each JobSpec
becomes exactly one job (one Kubernetes Job, or one local docker-compose run).
All work items in a job run sequentially inside a single simulator setup (the
simulator is reset between them), which amortises setup cost for simulators with
a cheap per-run cost (e.g. MuJoCo).

Packing is an axis independent of *which* configs exist (variation expansion)
and *how* a job is dispatched (local vs. cluster). Crucially, results stay keyed
by configuration name / run number regardless of how work items were packed, so
downstream reading and post-processing never need to know about packing.

Two packers are provided:

* :class:`OnePerJob` — one work item per job (one job == one config/run).
  The right choice when setup dominates and one job should be one scenario
  (e.g. Gazebo). This is the default.
* :class:`FixedK` — up to ``k`` work items per job.

Use :func:`build_jobs` to select and apply the packer from an execution config.
"""

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class WorkItem:
    """One configuration executed at one run number.

    Attributes:
        config: The configuration entry from ``campaign_data["configs"]``
            (carries ``name``, ``config`` params, ``_config_files``, …).
        run_number: The 0-based run index for this work item.
    """
    config: dict
    run_number: int

    @property
    def config_name(self) -> str:
        return self.config.get("name", "")


@dataclass
class JobSpec:
    """A planned job: an ordered group of work items that run together.

    The work items run sequentially in ONE job (one Kubernetes Job or one local
    docker-compose run), with a simulator reset between them.

    Attributes:
        items: The work items, in execution order.
        index: 0-based index of this job within the campaign (stable, used for
            job naming and progress display).
    """
    items: list[WorkItem]
    index: int = 0

    @property
    def config_names(self) -> list[str]:
        """Distinct config names in this job, in first-seen order."""
        seen: dict[str, None] = {}
        for it in self.items:
            seen.setdefault(it.config_name, None)
        return list(seen.keys())

    def __len__(self) -> int:
        return len(self.items)


class Packer:
    """Base class: group work items into jobs."""

    def pack(self, items: list[WorkItem]) -> list[JobSpec]:
        raise NotImplementedError


class OnePerJob(Packer):
    """One work item per job (one job == one config/run)."""

    def pack(self, items: list[WorkItem]) -> list[JobSpec]:
        return [JobSpec(items=[item], index=i) for i, item in enumerate(items)]


class FixedK(Packer):
    """Up to ``k`` work items per job (consecutive chunks)."""

    def __init__(self, k: int):
        if k < 1:
            raise ValueError(f"FixedK requires k >= 1, got {k}")
        self.k = k

    def pack(self, items: list[WorkItem]) -> list[JobSpec]:
        jobs = []
        for job_idx, start in enumerate(range(0, len(items), self.k)):
            jobs.append(JobSpec(items=items[start:start + self.k], index=job_idx))
        return jobs


def build_work_items(configs: list[dict], runs: int) -> list[WorkItem]:
    """Enumerate work items for a campaign.

    Items are grouped **by config, then by run** (``for config: for run``), so a
    config's repeated runs stay adjacent. With multi-config packing this keeps all
    runs of one configuration together within a job (prioritised over interleaving
    different configs), so a packed job runs ``config A`` runs 0..N before moving
    on to ``config B`` and a config's results land contiguously. (Packing remains
    invisible to result reading, which is keyed by config name / run number.)
    """
    items = []
    for config in configs:
        for run_number in range(runs):
            items.append(WorkItem(config=config, run_number=run_number))
    return items


def select_packer(execution_cfg: dict) -> Packer:
    """Select a packer from an execution config dict.

    ``configs_per_job`` (default 1) drives the choice: 1 selects
    :class:`OnePerJob` (one config per job — the historical behaviour); a value
    >1 selects :class:`FixedK`.
    """
    k = int(execution_cfg.get("configs_per_job") or 1)
    if k < 1:
        raise ValueError(f"execution.configs_per_job must be >= 1, got {k}")
    return FixedK(k) if k > 1 else OnePerJob()


def build_jobs(configs: list[dict], runs: int, execution_cfg: dict) -> list[JobSpec]:
    """Build jobs for a campaign from its configs, runs and execution config."""
    items = build_work_items(configs, runs)
    jobs = select_packer(execution_cfg).pack(items)
    logger.debug(
        "Packed %d config(s) x %d run(s) = %d work item(s) into %d job(s)",
        len(configs), runs, len(items), len(jobs),
    )
    return jobs
