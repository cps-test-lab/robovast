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

"""A campaign's jobs: the runs of one configuration, one or more per job.

A *run* is one configuration executed at one run number (one scenario execution), and a *job*
is the unit of dispatch (one Kubernetes Job): its own pod, its own simulator process and its
own ``/config``. By default a job is one run. ``execution.runs_per_job`` lets a job hold up to
that many runs **of one configuration**, executed one after another in the one simulator
process, which is reset between them -- what pays for a simulator whose setup dominates a run.

A job never mixes configurations: it holds one world and one configuration's files, so what it
can hold several of is that configuration's runs. Results are keyed by configuration name /
run number whatever the packing; the job index names the job's artifact directory under
``_jobs/``.
"""

from dataclasses import dataclass, field


@dataclass
class Job:
    """One or more runs of one configuration, dispatched as one job.

    Attributes:
        config: The configuration entry from ``campaign_data["configs"]``
            (carries ``name``, ``config`` params, ``_config_files``, …).
        runs: The 0-based run numbers, ascending, in execution order.
        index: 0-based index of this job within the campaign (stable, used for
            job naming and progress display).
    """
    config: dict
    runs: list[int] = field(default_factory=list)
    index: int = 0

    @property
    def config_name(self) -> str:
        return self.config.get("name", "")

    @property
    def first_run(self) -> int:
        return self.runs[0]

    @property
    def run_keys(self) -> tuple[str, ...]:
        """``<config>/<run>`` of every run, in execution order."""
        return tuple(f"{self.config_name}/{run}" for run in self.runs)


def declared_runs_per_job(execution_params: dict) -> int:
    """``execution.runs_per_job`` as declared, 1 when it is not."""
    return int((execution_params or {}).get("runs_per_job") or 1)


def job_count(num_configs: int, runs: int, runs_per_job: int = 1) -> int:
    """How many jobs :func:`build_jobs` makes of *num_configs* configurations at *runs* runs:
    what a validation or a preview reports before anything is dispatched. Counted by the
    same function, so the number a reader is shown is the number the backend creates."""
    return len(build_jobs([{"name": ""}] * num_configs, runs, runs_per_job))


def build_jobs(configs: list[dict], runs: int, runs_per_job: int = 1) -> list[Job]:
    """The jobs, **by config, then by run**: each configuration's runs in chunks of
    *runs_per_job*, so a configuration's runs are adjacent and a job holds one configuration.

    Deterministic: the jobs the per-job parameter files are written for and the jobs the
    manifests are created for come from separate calls, and must be the same jobs.
    """
    if runs_per_job < 1:
        raise ValueError(f"runs_per_job must be at least 1, got {runs_per_job}")
    jobs = []
    for config in configs:
        for start in range(0, runs, runs_per_job):
            jobs.append(Job(config=config, runs=list(range(start, min(start + runs_per_job, runs))),
                            index=len(jobs)))
    return jobs
