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

"""Structured config dataclasses for Hydra/OmegaConf.

These dataclasses define the schema for robovast configuration files.
They replace the old Pydantic models in robovast.common.config.
"""

from dataclasses import dataclass, field
from typing import Any, Optional

from omegaconf import MISSING


@dataclass
class ResourcesConfig:
    """Resource limits for a container."""
    cpu: Optional[int] = None
    memory: Optional[str] = None


@dataclass
class SecondaryContainerConfig:
    """A secondary container in the pod (e.g., gazebo)."""
    name: str = MISSING
    resources: Optional[ResourcesConfig] = None


@dataclass
class ExecutionConfig:
    """Execution configuration: image, resources, runs, scenario file."""
    image: str = MISSING
    resources: Optional[ResourcesConfig] = None
    secondary_containers: Optional[list[Any]] = None
    env: Optional[list[Any]] = None
    runs: int = 1
    scenario_file: Optional[str] = None
    run_files: Optional[list[str]] = None
    timeout: Optional[int] = None


@dataclass
class PostprocessingEntry:
    """A single postprocessing plugin entry."""
    # Stored as list[str | dict] in the config, not as this dataclass.
    # This is here for documentation; actual config uses Any.
    pass


@dataclass
class ResultsProcessingConfig:
    """Results processing configuration: postprocessing, metadata, publication."""
    postprocessing: Optional[list[Any]] = None
    metadata_processing: Optional[list[Any]] = None
    publication: Optional[list[Any]] = None


@dataclass
class EvaluationConfig:
    """Evaluation configuration: visualization notebooks."""
    visualization: Optional[list[Any]] = None


@dataclass
class MetadataConfig:
    """Campaign metadata."""
    name: str = MISSING


@dataclass
class RobovastConfig:
    """Top-level robovast configuration.

    This is the schema for both single-file configs (e.g., basic_nav.yaml)
    and composed configs (with Hydra defaults).

    Sections:
        metadata: Campaign name and description.
        scenario: Scenario parameters (sweepable).
        pipeline: Named callbacks for file generation (sweepable params).
        execution: Image, resources, runs, scenario file.
        results_processing: Postprocessing, metadata, publication plugins.
        evaluation: Analysis notebooks.
    """
    metadata: MetadataConfig = field(default_factory=MetadataConfig)
    scenario: Any = field(default_factory=dict)
    pipeline: Any = field(default_factory=dict)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    results_processing: Optional[ResultsProcessingConfig] = None
    evaluation: Optional[EvaluationConfig] = None
