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

"""A campaign's jobs: one run per job by default, several runs packed on request.

A *run* is one configuration executed at one run number (one scenario execution), and a *job*
is the unit of dispatch (one Kubernetes Job): its own pod, its own simulator process and its
own ``/config``. By default a job is one run. ``execution.runs_per_job`` lets a job hold up to
that many runs, executed one after another in the one simulator process, which is reset between
them -- what pays for a simulator whose setup dominates a run.

Which runs may share a job is decided by a **pod key** per configuration: what the pod is made
of, as the backend resolves it. Configurations of equal key would get the same pod, so their
runs are laid out together, configuration after configuration (*config-major*), and cut into
jobs; a configuration whose key nobody shares keeps jobs of its own. This module knows nothing
of what a key is made of -- the caller supplies one per configuration -- only that equal keys
may share.

Results are keyed by configuration name / run number whatever the packing; the job index names
the job's artifact directory under ``_jobs/``.
"""

from collections.abc import Hashable, Mapping
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RunItem:
    """One run: a configuration entry from ``campaign_data["configs"]`` at a run number."""
    config: dict
    run: int

    @property
    def config_name(self) -> str:
        return self.config.get("name", "")

    @property
    def key(self) -> str:
        """``<config>/<run>``, the run's result directory relative to the campaign root."""
        return f"{self.config_name}/{self.run}"


@dataclass
class Job:
    """An ordered list of runs dispatched as one job, config-major.

    Attributes:
        items: The runs in execution order. Runs of one configuration are adjacent and
            ascending, and a configuration's items come before the next's.
        index: 0-based index of this job within the campaign (stable, used for job naming and
            progress display).
    """
    items: list[RunItem] = field(default_factory=list)
    index: int = 0

    @classmethod
    def of_runs(cls, config: dict, runs: list[int], index: int = 0) -> "Job":
        """A job of one configuration's *runs*."""
        return cls(items=[RunItem(config, run) for run in runs], index=index)

    @property
    def first(self) -> RunItem:
        return self.items[0]

    @property
    def first_run(self) -> int:
        return self.first.run

    @property
    def runs(self) -> list[int]:
        """The run numbers, in execution order."""
        return [item.run for item in self.items]

    @property
    def run_keys(self) -> tuple[str, ...]:
        """``<config>/<run>`` of every run, in execution order."""
        return tuple(item.key for item in self.items)

    @property
    def configs(self) -> list[dict]:
        """The configurations this job holds runs of, in first-seen order, each once."""
        seen = []
        for item in self.items:
            if not any(item.config is c or item.config == c for c in seen):
                seen.append(item.config)
        return seen

    @property
    def config_names(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(item.config_name for item in self.items))


def declared_runs_per_job(execution_params: dict) -> int:
    """``execution.runs_per_job`` as declared, 1 when it is not."""
    return int((execution_params or {}).get("runs_per_job") or 1)


def _groups(configs: list[dict], runs_per_job: int,
            keys: Optional[Mapping[str, Hashable]]) -> list[list[dict]]:
    """The configurations grouped by equal pod key, in first-seen order.

    At ``runs_per_job == 1`` no run shares a job, so every configuration is a group of its
    own and no key is asked for. Above it a key is required for every configuration: a
    missing one would silently pack it with nothing or with everything.
    """
    if runs_per_job == 1:
        return [[config] for config in configs]
    if keys is None:
        raise ValueError("runs_per_job above 1 needs a pod key per configuration")
    by_key: dict = {}
    for config in configs:
        name = config.get("name", "")
        if name not in keys:
            raise ValueError(f"no pod key for configuration {name!r}")
        by_key.setdefault(keys[name], []).append(config)
    return list(by_key.values())


def build_jobs(configs: list[dict], runs: int, runs_per_job: int = 1,
               keys: Optional[Mapping[str, Hashable]] = None) -> list[Job]:
    """The jobs, config-major within groups of equal pod key, in first-seen order.

    Each group's runs -- every run of its first configuration, then of its next -- are cut
    into jobs of up to *runs_per_job* items, so a job never crosses a group, a configuration's
    runs are adjacent, and a job of several configurations changes configuration as rarely as
    the cut allows. *keys* maps a configuration's name to its pod key; it is required above
    ``runs_per_job == 1`` and ignored at it, where the plan is one job per run.

    Deterministic: the jobs the per-job parameter files are written for and the jobs the
    manifests are created for come from separate calls, and must be the same jobs.
    """
    if runs_per_job < 1:
        raise ValueError(f"runs_per_job must be at least 1, got {runs_per_job}")
    jobs = []
    for group in _groups(configs, runs_per_job, keys):
        items = [RunItem(config, run) for config in group for run in range(runs)]
        for start in range(0, len(items), runs_per_job):
            jobs.append(Job(items=items[start:start + runs_per_job], index=len(jobs)))
    return jobs


def job_count(configs: list[dict], runs: int, runs_per_job: int = 1,
              keys: Optional[Mapping[str, Hashable]] = None) -> int:
    """How many jobs :func:`build_jobs` makes: what a validation or a preview reports before
    anything is dispatched. Counted by the same function, so the number a reader is shown is
    the number the backend creates."""
    return len(build_jobs(configs, runs, runs_per_job, keys))


#: Why a configuration keeps a pod of its own when nothing in the key maker says so: its pod is
#: simply not anyone else's.
OWN_OVERLAY_REASON = "its simulator overlay is its own"


def pod_statements(configs: list[dict], runs: int, runs_per_job: int = 1,
                   keys: Optional[Mapping[str, Hashable]] = None,
                   own_reasons: Optional[Mapping[str, str]] = None) -> list[str]:
    """One sentence per pod group, in first-seen order: which configurations share a pod, and
    for one that keeps its own, why. Empty at ``runs_per_job == 1``, where there is no
    sharing to speak of.

    *own_reasons* names, per configuration, why its key maker gave it a key nobody shares
    (``"stages a file of its own"``); a lone configuration without one is lone because its
    overlay differs from every other's.
    """
    if runs_per_job == 1:
        return []
    statements = []
    for group in _groups(configs, runs_per_job, keys):
        names = [config.get("name", "") for config in group]
        if len(names) > 1:
            jobs = -(-len(names) * runs // runs_per_job)
            statements.append(
                f"{', '.join(names)} share a pod ({jobs} job(s) of up to {runs_per_job} runs)")
        else:
            reason = (own_reasons or {}).get(names[0]) or OWN_OVERLAY_REASON
            statements.append(f"{names[0]} keeps its own pod: {reason}")
    return statements
