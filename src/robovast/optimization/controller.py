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

"""Optimization controller abstraction.

Defines where the optimization loop runs: on the user's workstation
(LocalController) or in the cluster (InClusterController, future).
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class OptimizationResult:
    """Result of an optimization run."""
    best_params: dict = field(default_factory=dict)
    best_value: float = 0.0
    n_trials: int = 0
    study: Any = None


class OptimizationController(ABC):
    """Abstract base for optimization loop controllers."""

    @abstractmethod
    def run(self, config: Any, launcher: Any) -> OptimizationResult:
        """Run the optimization loop.

        Args:
            config: Hydra DictConfig with sweeper configuration.
            launcher: K8sLauncher or LocalLauncher instance.

        Returns:
            Optimization result with best parameters.
        """
        ...


class LocalController(OptimizationController):
    """Runs the optimization loop on the user's workstation.

    Polls the K8s API for job completion between iterations.
    This is the default controller for now.
    """

    def run(self, config, launcher) -> OptimizationResult:
        # The Optuna sweeper drives the loop; the controller
        # just provides the execution environment.
        # Full implementation depends on wiring the Optuna sweeper
        # with the K8sLauncher.
        raise NotImplementedError(
            "LocalController.run: will be implemented when Optuna "
            "sweeper integration is complete."
        )
