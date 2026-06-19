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

"""Execution backends for the campaign controller.

A backend runs **one batch** of jobs and is otherwise dumb: the
:class:`~robovast.execution.controller.CampaignController` owns the campaign
lifecycle (campaign id, results layout, store, the batch loop and scoring); a
backend only dispatches a batch's jobs so results land at
``<campaign_root>/<config>/<run>/``.

:class:`DockerBackend` is the local backend; it reuses the existing
docker-compose run-script generation but executes each batch **into a fixed
campaign root** (no per-batch campaign-id nesting). A ``KubernetesBackend`` with
the same interface can be added later to drive cluster batch and search through
the same controller.
"""

import logging
import os
import re
import subprocess  # nosec - invokes the generated, trusted robovast run script
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass

from robovast.common import prepare_campaign_configs
from robovast.common.execution import (DEFAULT_ROBOVAST_IMAGE,
                                       resolve_robovast_image)
from robovast.execution.execution_utils.execute_local import \
    generate_compose_run_script

logger = logging.getLogger(__name__)


@dataclass
class RunOptions:
    """Per-run execution options (mostly local docker-compose specific)."""
    gui: bool = False
    start_only: bool = False
    network_host: bool = False
    abort_on_failure: bool = False
    # None ⇒ resolve via resolve_robovast_image() (config / ROBOVAST_IMAGE / default);
    # a non-None value is an explicit ``--image`` and wins over everything.
    image: str | None = None
    log_tree: bool = False
    debug: bool = False
    skip_resource_allocation: bool = True


class ExecutionBackend(ABC):
    """Runs one batch's jobs; results land at ``<campaign_root>/<config>/<run>/``.

    Results stay keyed by config name / run number regardless of how the backend
    packs or dispatches, so the controller's scoring and the store are unaffected
    by the backend choice.
    """

    @abstractmethod
    def run_batch(self, campaign_data: dict, *, campaign_root: str, batch_tag: str,
                  runs: int, options: RunOptions) -> None:
        """Execute the jobs for ``campaign_data`` into ``campaign_root``.

        ``batch_tag`` (e.g. ``"batch-3"``) namespaces job-level artifacts so
        multiple batches sharing one campaign root do not collide.
        """


def _sanitize(tag: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", tag)


def stage_run_script(campaign_data: dict, work_dir: str, runs: int,
                     options: "RunOptions", *, job_prefix: str = "",
                     results_dir: str = "") -> str:
    """Prepare a batch's configs + ``run.sh`` under ``work_dir`` (no execution).

    Writes ``work_dir/out_template/`` (the prepared config tree) and a
    ``work_dir/run.sh`` docker-compose runner whose baked default results dir is
    ``results_dir/<campaign-id>``. Returns the ``run.sh`` path. Shared by
    :class:`DockerBackend` (staging into a temp dir, then running) and
    ``prepare-run`` (staging into a persistent, inspectable directory).
    """
    execution = campaign_data.get("execution", {})
    image = resolve_robovast_image(explicit=options.image,
                                   config_image=execution.get("image"))
    config_path_result = os.path.join(work_dir, "out_template")
    prepare_campaign_configs(config_path_result, campaign_data)

    run_script = os.path.join(work_dir, "run.sh")
    generate_compose_run_script(
        runs, campaign_data, config_path_result,
        execution.get("pre_command"), execution.get("post_command"),
        image, results_dir, run_script,
        skip_resource_allocation=options.skip_resource_allocation,
        log_tree=options.log_tree, debug=options.debug, job_prefix=job_prefix)
    return run_script


class DockerBackend(ExecutionBackend):
    """Local backend: run a batch via docker compose into the campaign root.

    Reuses :func:`generate_compose_run_script`, but invokes the generated script
    with ``--campaign-dir <campaign_root>`` so the batch writes directly into the
    campaign root (the controller owns the campaign id). The simulator-side
    ``entrypoint.sh`` is unchanged.
    """

    def run_batch(self, campaign_data: dict, *, campaign_root: str, batch_tag: str,
                  runs: int, options: RunOptions) -> None:
        os.makedirs(campaign_root, exist_ok=True)
        image = resolve_robovast_image(
            explicit=options.image,
            config_image=campaign_data.get("execution", {}).get("image"))

        # Stage the prepared configs + run.sh in a temp dir (not the results dir);
        # run.sh copies out_template into the campaign root, so only results +
        # campaign metadata remain there. The temp dir is removed afterwards.
        with tempfile.TemporaryDirectory(prefix=f"robovast_{_sanitize(batch_tag)}_") as work_dir:
            run_script = stage_run_script(
                campaign_data, work_dir, runs, options,
                job_prefix=batch_tag, results_dir=campaign_root)

            cmd = [run_script, "--campaign-dir", os.path.abspath(campaign_root)]
            if not options.gui:
                cmd.append("--no-gui")
            if options.start_only:
                cmd.append("--start-only")
            if options.network_host:
                cmd.append("--network-host")
            if options.abort_on_failure:
                cmd.append("--abort-on-failure")
            if image != DEFAULT_ROBOVAST_IMAGE:
                cmd.extend(["--image", image])

            logger.info("Launching batch %s: %s", batch_tag, " ".join(cmd))
            # NOT check=True: in a failure-finding run, scenario runs are *meant*
            # to fail and a non-zero exit is the signal; the controller reads the
            # per-config results either way. --abort-on-failure changes the
            # script's own behaviour, not ours.
            result = subprocess.run(cmd)  # nosec - generated, trusted run script
        if result.returncode != 0:
            logger.warning(
                "Batch %s run script exited with code %d (some runs failed); "
                "continuing to evaluate produced results.", batch_tag, result.returncode)
