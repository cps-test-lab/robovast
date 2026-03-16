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

import logging
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
    ) -> dict:
        """Execute a single resolved config locally via Docker.

        Args:
            cfg: Fully resolved Hydra config.
            ctx: Pipeline context with generated files.
            output_dir: Directory for output.

        Returns:
            Results dict.
        """
        execution = OmegaConf.to_container(cfg.execution, resolve=True)
        image = execution["image"]
        logger.info("Launching local Docker execution with image: %s", image)

        # This will integrate with the existing local execution infrastructure
        # in robovast.execution.local_execution.
        raise NotImplementedError(
            "LocalLauncher.launch: wiring to Docker execution pending. "
            "This will be implemented when the local execution module is "
            "adapted to accept PipelineContext."
        )
