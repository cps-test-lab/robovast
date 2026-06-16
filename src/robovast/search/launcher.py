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

"""Launchers run one generation's composed configs and block until done.

A launcher is deliberately dumb: dispatch + completion only. Packing happens via
the existing :func:`build_jobs`, and scoring stays in the evaluator/loop. The
:class:`LocalLauncher` reuses the same config preparation and run-script
generation as ``vast execution local run``; an in-cluster launcher can implement
the same :meth:`Launcher.launch` contract later.
"""

import logging
import os
import subprocess  # nosec - invokes the generated robovast run script
from abc import ABC, abstractmethod
from pathlib import Path

from robovast.common import prepare_campaign_configs
from robovast.execution.execution_utils.execute_local import generate_compose_run_script

logger = logging.getLogger(__name__)


class Launcher(ABC):
    """Runs the configs in ``campaign_data`` and writes per-config results.

    Results must land at ``<result_dir>/<config-name>/<run>/`` so the evaluator
    can read them regardless of how work items were packed.
    """

    @abstractmethod
    def launch(self, campaign_data: dict, gen_dir: str, runs: int) -> str:
        """Execute one generation; return the directory holding per-config results."""


class LocalLauncher(Launcher):
    """Executes a generation locally via Docker, reusing the batch run script."""

    def __init__(self, skip_resource_allocation: bool = True, extra_args: list[str] | None = None):
        self.skip_resource_allocation = skip_resource_allocation
        # --no-gui by default: search loops are headless.
        self.extra_args = extra_args if extra_args is not None else ["--no-gui"]

    def launch(self, campaign_data: dict, gen_dir: str, runs: int) -> str:
        execution = campaign_data.get("execution", {})
        image = execution.get("image", "ghcr.io/cps-test-lab/robovast:latest")
        pre_command = execution.get("pre_command")
        post_command = execution.get("post_command")

        config_path_result = os.path.join(gen_dir, "out_template")
        result_dir = os.path.join(gen_dir, "results")
        os.makedirs(result_dir, exist_ok=True)
        prepare_campaign_configs(config_path_result, campaign_data)

        run_script = os.path.join(gen_dir, "run.sh")
        generate_compose_run_script(
            runs, campaign_data, config_path_result, pre_command, post_command,
            image, result_dir, run_script,
            skip_resource_allocation=self.skip_resource_allocation,
        )

        cmd = [run_script, "--results-dir", os.path.abspath(result_dir), *self.extra_args]
        logger.info("Launching generation: %s", " ".join(cmd))
        # NOT check=True: in a failure-finding search, scenario runs are *meant*
        # to fail. A non-zero exit means some run failed, which is the signal we
        # want — the evaluator reads the per-config results either way. Only a
        # missing run script (setup error) is fatal.
        result = subprocess.run(cmd)  # nosec - generated, trusted run script
        if result.returncode != 0:
            logger.warning(
                "Generation run script exited with code %d (some runs failed); "
                "continuing to evaluate produced results.", result.returncode)
        # The run script nests results under a RUN_ID (<campaign-name>-<timestamp>)
        # subdirectory of --results-dir. Return that campaign dir so per-config
        # results resolve at <campaign>/<config>/<run>/.
        subdirs = [d for d in Path(result_dir).iterdir() if d.is_dir()] if os.path.isdir(result_dir) else []
        if not subdirs:
            logger.warning("No campaign output found under %s", result_dir)
            return result_dir
        return str(max(subdirs, key=lambda d: d.stat().st_mtime))
