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

"""Pipeline executor — runs the callback chain for one resolved config.

The pipeline runs INSIDE each Hydra job, after Hydra has resolved all
parameters (including callback parameters like seed, path_length).
Each callback produces exactly ONE set of output files.

Usage::

    from robovast.pipeline.executor import run_pipeline

    ctx = run_pipeline(cfg, output_dir=Path("/tmp/campaign"))
    # ctx.scenario_params has the fully resolved parameters
    # ctx.generated_files has all generated file paths
"""

import logging
import os
from pathlib import Path

import hydra.utils
from omegaconf import DictConfig, OmegaConf

from .callback import PipelineCallback
from .context import PipelineContext

logger = logging.getLogger(__name__)


def run_pipeline(cfg: DictConfig, output_dir: Path) -> PipelineContext:
    """Run the callback chain for ONE resolved config.

    Hydra has already set all callback parameters (seeds, path_length,
    etc.) via the sweeper. Each callback generates exactly 1 set of
    output files.

    Args:
        cfg: The fully resolved Hydra config for this job.
        output_dir: Directory where callbacks write generated artifacts.

    Returns:
        PipelineContext with resolved scenario params and generated files.
    """
    scenario = OmegaConf.to_container(cfg.scenario, resolve=True)
    scenario_name = scenario.get("name", "config")

    config_dir = os.path.dirname(
        cfg.get("_config_dir", os.getcwd())
    ) if "_config_dir" in cfg else os.getcwd()

    execution = cfg.get("execution", {})
    scenario_file_name = OmegaConf.to_container(execution).get("scenario_file") if execution else None
    scenario_file = Path(config_dir) / scenario_file_name if scenario_file_name else Path()

    ctx = PipelineContext(
        scenario_params=scenario,
        generated_files={},
        config_files=[],
        transient_files=[],
        execution=execution,
        general=cfg.get("general", {}),
        scenario_file=scenario_file,
        output_dir=output_dir,
        base_path=Path(config_dir),
        scenario_name=scenario_name,
    )

    os.makedirs(output_dir, exist_ok=True)

    pipeline_cfg = cfg.get("pipeline", {})
    if not pipeline_cfg:
        logger.debug("No pipeline callbacks defined, returning base config")
        return ctx

    for name, callback_cfg in pipeline_cfg.items():
        if not isinstance(callback_cfg, DictConfig):
            continue
        if "_target_" not in callback_cfg:
            continue

        logger.info("Running pipeline callback: %s", name)
        callback = hydra.utils.instantiate(callback_cfg, _convert_="partial")

        if not isinstance(callback, PipelineCallback):
            raise TypeError(
                f"Pipeline callback '{name}' ({type(callback).__name__}) "
                f"must be a subclass of PipelineCallback"
            )

        ctx = callback.execute(ctx)
        logger.info("Callback '%s' completed. Generated files: %s",
                     name, list(ctx.generated_files.keys()))

    return ctx
