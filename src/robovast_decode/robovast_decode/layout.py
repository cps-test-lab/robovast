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

#: PyYAML's C parser where it is built: the job-link manifest is read on every query and every
#: recording's ``metadata.yaml`` on every listing, and the pure-Python parser spends most of their
#: time on a campaign of hundreds of runs.
YAML_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)

#: What the main container is called. The runtime container is ``robovast`` (the pod's
#: container) -- NOT ``scenario``, which is the container plan's
#: *role* name. Every reader that names a container agrees on it: the live job log tags its
#: lines with it, and ``run_log``, ``resource_usage`` and ``system_usage`` file their rows under
#: it, so one campaign reads as one set of containers on every surface.
MAIN_CONTAINER = "robovast"

#: The campaign's store, written by the controller.
STORE = "campaign.db"

#: The campaign's ``{"<config>/<run>/job": "<job dir relative to the run>"}`` manifest, written
#: before a job starts (the ``job`` symlink beside a run appears only once it ends).
JOB_LINKS = os.path.join("_transient", "job_links.yaml")

#: The campaign's intervention ledger.
INTERVENTIONS = os.path.join("_execution", "interventions.json")

#: How the campaign's recordings become tables where it refines the defaults, in the decoder's
#: configuration shape (``{"groups": [{"bag_dir": ..., "plugins": [...]}]}``): written by the
#: controller from the campaign's ``.vast``, so a copy of the campaign carries it.
DECODER_CONFIG = os.path.join("_execution", "tables.yaml")


def job_links(campaign_dir: str) -> dict:
    """The job-link manifest; ``{}`` for a campaign that has none."""
    path = os.path.join(campaign_dir, JOB_LINKS)
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        return yaml.load(fh, Loader=YAML_LOADER) or {}


def decoder_config(campaign_dir: str) -> dict:
    """The campaign's decoder configuration; ``{}`` when it refines no default."""
    path = os.path.join(campaign_dir, DECODER_CONFIG)
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        config = yaml.load(fh, Loader=YAML_LOADER) or {}
    if not isinstance(config, dict):
        raise ValueError(f"{path}: expected a mapping with 'groups', got {type(config).__name__}")
    return config


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


__all__ = ["DECODER_CONFIG", "INTERVENTIONS", "JOB_LINKS", "MAIN_CONTAINER", "STORE",
           "decoder_config", "job_links", "run_dirs"]
