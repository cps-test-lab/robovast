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

"""Objective function interface for optimization loops.

Users implement this to define what metric to optimize.
Concrete implementations are deferred — this defines the interface.

Example usage in Hydra config::

    hydra:
      sweeper:
        objective:
          _target_: my_project.objectives.NavSuccessRate
          weight_duration: 0.3
"""

from abc import ABC, abstractmethod
from pathlib import Path


class ObjectiveFunction(ABC):
    """Interface for optimization objectives.

    Implementations receive a results directory (with postprocessed
    data) and return a scalar metric to minimize or maximize.
    """

    @abstractmethod
    def evaluate(self, results_dir: Path) -> float:
        """Evaluate the objective on campaign results.

        Args:
            results_dir: Path to the campaign results directory,
                containing postprocessed CSVs, data.db, etc.

        Returns:
            Scalar metric value. The optimization direction
            (minimize/maximize) is configured in the sweeper config.
        """
        ...
