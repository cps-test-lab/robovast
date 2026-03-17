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

"""Local Docker launcher for Hydra.

Runs a single resolved config locally using Docker.
1 Hydra job = 1 Docker run.
"""

import glob as glob_module
import logging
import os
import subprocess
from pathlib import Path

from omegaconf import DictConfig, OmegaConf

from robovast.pipeline.context import PipelineContext

logger = logging.getLogger(__name__)


class LocalLauncher:
    """Hydra launcher for local Docker execution.

    1 Hydra job = 1 Docker container run (1:1 mapping).
    """

    def launch(
        self,
        cfg: DictConfig,
        ctx: PipelineContext,
        output_dir: Path,
    ) -> int:
        """Execute a single resolved config locally via Docker.

        Generates a docker-compose run script and executes it.
        Results are written into output_dir.

        Args:
            cfg: Fully resolved Hydra config.
            ctx: Pipeline context with generated files and scenario params.
            output_dir: Campaign directory where results are written.

        Returns:
            Exit code from the run script.
        """
        from robovast.common import prepare_campaign_configs
        from robovast.execution.execution_utils.execute_local import generate_compose_run_script

        execution = OmegaConf.to_container(cfg.execution, resolve=True)
        metadata = OmegaConf.to_container(cfg.get("metadata", {}), resolve=True)

        image = execution.get("image", "ghcr.io/cps-test-lab/robovast:latest")
        runs = execution.get("runs", 1)
        pre_command = execution.get("pre_command")
        post_command = execution.get("post_command")

        # "vast" is used by prepare_campaign_configs as os.path.dirname(vast) → config dir
        config_dir = os.path.abspath(cfg.get("_config_dir", os.getcwd()))

        # Expand glob patterns in run_files against the config directory
        expanded_run_files = []
        for pattern in execution.get("run_files", []):
            matches = glob_module.glob(os.path.join(config_dir, pattern))
            for match in sorted(matches):
                expanded_run_files.append(os.path.relpath(match, config_dir))

        config_file = cfg.get("_config_file", os.path.join(config_dir, "config.yaml"))
        # "name" is the config name, not a scenario parameter — exclude it from the
        # config dict so it isn't written to scenario.config as a parameter override.
        scenario_config = {k: v for k, v in ctx.scenario_params.items() if k != "name"}
        campaign_data = {
            "configs": [{"name": ctx.scenario_name, "config": scenario_config}],
            "execution": execution,
            "metadata": metadata,
            "_run_files": expanded_run_files,
            "scenario_file": execution.get("scenario_file", "scenario.osc"),
            "vast": config_file,
        }

        output_dir = Path(output_dir).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)

        config_template = str(output_dir / "out_template")
        prepare_campaign_configs(config_template, campaign_data)

        run_script = str(output_dir / "run.sh")
        generate_compose_run_script(
            runs=runs,
            campaign_data=campaign_data,
            config_path_result=config_template,
            pre_command=pre_command,
            post_command=post_command,
            docker_image=image,
            results_dir=str(output_dir),
            output_script_path=run_script,
            fixed_results_dir=str(output_dir),
        )

        logger.info("Executing local run script: %s", run_script)
        result = subprocess.run([run_script], check=False)
        return result.returncode
