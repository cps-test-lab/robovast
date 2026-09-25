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

"""Where a campaign directory keeps what the tables are built from."""

from __future__ import annotations

import os

import yaml

#: The campaign's store, written by the controller.
STORE = "campaign.db"

#: The campaign's ``{"<config>/<run>/job": "<job dir relative to the run>"}`` manifest, written
#: before a job starts (the ``job`` symlink beside a run appears only once it ends).
JOB_LINKS = os.path.join("_transient", "job_links.yaml")

#: The campaign's intervention ledger.
INTERVENTIONS = os.path.join("_execution", "interventions.json")


def job_links(campaign_dir: str) -> dict:
    """The job-link manifest; ``{}`` for a campaign that has none."""
    path = os.path.join(campaign_dir, JOB_LINKS)
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def run_dirs(campaign_dir: str):
    """``[(config_name, run_id)]`` of every ``<config>/<numeric run>`` directory, in order."""
    out = []
    for config in sorted(os.listdir(campaign_dir)):
        path = os.path.join(campaign_dir, config)
        if config.startswith(("_", ".")) or not os.path.isdir(path):
            continue
        out.extend((config, int(run)) for run in sorted((r for r in os.listdir(path)
                                                          if r.isdigit()), key=int)
                   if os.path.isdir(os.path.join(path, run)))
    return out


__all__ = ["INTERVENTIONS", "JOB_LINKS", "STORE", "job_links", "run_dirs"]
