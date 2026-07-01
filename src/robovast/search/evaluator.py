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

"""Scoring: per-config result directory -> :class:`Evaluation`.

Strategy-independent. Instantiates the one configured :class:`Extractor` (built-in
or a local file, parameterized from the ``.vast``), runs it per config, and wraps
its objectives + measures into an :class:`Evaluation`. The framework counts
``n_samples`` so it always matches what the extractor aggregated over.
"""

import logging
from pathlib import Path

from robovast.common.config import SearchConfig

from .extractor import Extractor, completed_run_dirs
from .plugins import EXTRACTOR_GROUP, load_ref
from .types import Evaluation, ParamSet

logger = logging.getLogger(__name__)


class Evaluator:
    """Applies the configured extractor to score parameter sets."""

    def __init__(self, cfg: SearchConfig, vast_dir: str = ""):
        extractor_cls = load_ref(cfg.extract.plugin, EXTRACTOR_GROUP, vast_dir)
        self.extractor: Extractor = extractor_cls(**cfg.extract.params)
        self.objective_names = [o.name for o in cfg.objectives]

    def evaluate(self, config_dir: Path, params: ParamSet) -> Evaluation:
        result = self.extractor.extract(config_dir)
        missing = [n for n in self.objective_names if n not in result.objectives]
        if missing:
            raise ValueError(
                f"Extractor did not return configured objective(s) {missing} for "
                f"{config_dir}; it returned {sorted(result.objectives)}")
        n_samples = len(completed_run_dirs(config_dir))
        logger.debug("Evaluated %s -> objectives=%s measures=%s n=%d",
                     params.id, result.objectives, result.measures, n_samples)
        return Evaluation(params=params, objectives=result.objectives,
                          measures=result.measures, n_samples=n_samples,
                          raw={"config_dir": str(config_dir)})
