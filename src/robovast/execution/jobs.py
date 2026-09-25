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

"""A campaign's jobs: one per run.

A *run* is one configuration executed at one run number (one scenario execution), and every
run is dispatched as its own job (one Kubernetes Job), with its own simulator process and its
own ``/config``. Results are keyed by configuration name / run number; the job index names the
job's artifact directory under ``_jobs/``.
"""

from dataclasses import dataclass


@dataclass
class Job:
    """One run, dispatched as one job.

    Attributes:
        config: The configuration entry from ``campaign_data["configs"]``
            (carries ``name``, ``config`` params, ``_config_files``, …).
        run_number: The 0-based run index.
        index: 0-based index of this job within the campaign (stable, used for
            job naming and progress display).
    """
    config: dict
    run_number: int
    index: int = 0

    @property
    def config_name(self) -> str:
        return self.config.get("name", "")


def build_jobs(configs: list[dict], runs: int) -> list[Job]:
    """One job per run, **by config, then by run**, so a configuration's runs are adjacent.

    Deterministic: the jobs the per-job parameter files are written for and the jobs the
    manifests are created for come from separate calls, and must be the same jobs.
    """
    return [Job(config=config, run_number=run_number, index=index)
            for index, (config, run_number) in enumerate(
                (config, run_number) for config in configs for run_number in range(runs))]
