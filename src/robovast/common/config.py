# Copyright (C) 2025 Frederik Pasch
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

import logging
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import (BaseModel, ConfigDict, Field, ValidationError,
                      field_validator, model_validator)

logger = logging.getLogger(__name__)


class GeneralConfig(BaseModel):
    model_config = ConfigDict(extra='allow')


class VariationConfig(BaseModel):
    pass
    # model_config = ConfigDict(extra='forbid')


class ScenarioParameterConfig(BaseModel):
    model_config = ConfigDict(extra='allow')


class ConfigurationConfig(BaseModel):
    name: str
    parameters: Optional[list[ScenarioParameterConfig]] = None
    variations: Optional[list[VariationConfig]] = None

    @field_validator('name')
    @classmethod
    def validate_name_no_invalid_characters(cls, v: str) -> str:
        if not v.islower():
            raise ValueError(f'name {v} must be all lowercase')
        if '_' in v or ' ' in v or '.' in v:
            raise ValueError(f'name {v}must not contain underscores, spaces, or periods')
        return v


class ResourcesConfig(BaseModel):
    """Resource limits for a container.

    Each field accepts either a plain scalar (the default, works for all
    clusters) or a per-cluster list when different clusters need different
    allocations::

        # Simple – same for every cluster
        resources:
          cpu: 8
          memory: 16Gi

        # Per-cluster – keys are the real Kubernetes context names
        resources:
          cpu:
            - gke_my-project_us-central1_my-cluster: 4
            - minikube: 8
          memory:
            - gke_my-project_us-central1_my-cluster: 10Gi
            - minikube: 20Gi
    """
    cpu: Optional[Union[int, list[dict[str, int]]]] = None
    memory: Optional[Union[str, list[dict[str, str]]]] = None


class SecondaryContainerConfig(BaseModel):
    name: str
    resources: Optional[ResourcesConfig] = None

    @model_validator(mode='before')
    @classmethod
    def extract_name(cls, data: Any) -> Any:
        if isinstance(data, str):
            return {'name': data, 'resources': None}
        if isinstance(data, dict):
            name = next((k for k in data if k != 'resources'), None)
            if name is None:
                raise ValueError("Secondary container entry must have a name key alongside 'resources'")
            resources = data.get('resources') or None
            return {'name': name, 'resources': resources}
        return data


def normalize_secondary_containers(secondary_containers) -> list[dict]:
    """Normalize secondary container entries to a uniform dict format with 'name' and 'resources' keys.

    Handles three input shapes:
    - Pydantic SecondaryContainerConfig objects (with .name / .resources attributes)
    - Already-normalized dicts with a 'name' key
    - Raw YAML dicts of the form {<container_name>: None, 'resources': {...}}
    """
    result = []
    for sc in (secondary_containers or []):
        if hasattr(sc, 'name'):
            result.append({
                'name': sc.name,
                'resources': {'cpu': sc.resources.cpu, 'memory': sc.resources.memory}
                if sc.resources is not None else {}
            })
        elif isinstance(sc, dict) and 'name' in sc:
            result.append(sc)
        elif isinstance(sc, dict):
            # Raw YAML format: {<name>: None, 'resources': {...}}
            name = next((k for k in sc if k != 'resources'), None)
            if name is None:
                raise ValueError(f"Cannot extract container name from secondary_containers entry: {sc}")
            result.append({'name': name, 'resources': sc.get('resources') or {}})
    return result


class ExecutionConfig(BaseModel):
    image: str
    resources: Optional[ResourcesConfig] = None
    secondary_containers: Optional[list[SecondaryContainerConfig]] = None
    env: Optional[list[dict[str, str]]] = None
    runs: int
    scenario_file: Optional[str] = None
    run_files: Optional[list[str]] = None
    timeout: Optional[int] = None  # Maximum execution time in seconds per run
    # Job packing. ``runs_per_job`` is how many runs (a run = one configuration
    # at one run-number) are packed into a single job:
    #   1 (default): each job runs exactly one run. Right for simulators where
    #     setup dominates the execution time, one job == one scenario (e.g. Gazebo).
    #   >1: up to N runs are packed into one job and run sequentially inside a
    #     single simulator setup (the simulator is reset between them), amortising
    #     setup for simulators with cheap per-run cost (e.g. MuJoCo). Runs are
    #     packed config-major, so a config's repeated runs stay together in a job.
    # Results stay keyed by configuration name / run number regardless, so packing
    # is invisible to downstream processing.
    runs_per_job: int = 1

    @field_validator('env')
    @classmethod
    def validate_no_reserved_env_vars(cls, v: Optional[list[dict[str, str]]]) -> Optional[list[dict[str, str]]]:
        """Validate that env does not contain reserved environment variable names."""
        if v is None:
            return v

        # Reserved keys that are set automatically during execution
        reserved_keys = {
            'CAMPAIGN_ID', 'ROS_LOG_DIR',
            'PRE_COMMAND', 'POST_COMMAND',
        }

        found_reserved = []
        for env_item in v:
            if isinstance(env_item, dict):
                for key in env_item.keys():
                    if key in reserved_keys:
                        found_reserved.append(key)

        if found_reserved:
            raise ValueError(
                f"execution.env contains reserved environment variable names: {', '.join(found_reserved)}. "
                f"Reserved names are: {', '.join(sorted(reserved_keys))}"
            )

        return v

    @field_validator('runs_per_job')
    @classmethod
    def validate_runs_per_job(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"execution.runs_per_job must be >= 1, got {v}")
        return v


class ResultsConfig(BaseModel):
    postprocessing: Optional[list[str | dict[str, Any]]] = None
    metadata_processing: Optional[list[str | dict[str, Any]]] = None
    publication: Optional[list[str | dict[str, Any]]] = None


class EvaluationConfig(BaseModel):
    visualization: Optional[list[dict[str, Any]]] = None


class FloatDim(BaseModel):
    """A continuous search dimension sampled from ``[low, high]``."""
    model_config = ConfigDict(extra='forbid')
    type: Literal['float']
    low: float
    high: float
    log: bool = False

    @model_validator(mode='after')
    def _check_bounds(self):
        if self.high < self.low:
            raise ValueError(f"float dim requires high >= low, got low={self.low}, high={self.high}")
        if self.log and self.low <= 0:
            raise ValueError("log-scaled float dim requires low > 0")
        return self


class IntDim(BaseModel):
    """A discrete integer search dimension sampled from ``[low, high]``."""
    model_config = ConfigDict(extra='forbid')
    type: Literal['int']
    low: int
    high: int
    log: bool = False
    step: Optional[int] = None

    @model_validator(mode='after')
    def _check_bounds(self):
        if self.high < self.low:
            raise ValueError(f"int dim requires high >= low, got low={self.low}, high={self.high}")
        if self.step is not None and self.step < 1:
            raise ValueError(f"int dim step must be >= 1, got {self.step}")
        if self.log and self.low <= 0:
            raise ValueError("log-scaled int dim requires low > 0")
        return self


class ChoiceDim(BaseModel):
    """A categorical search dimension sampled uniformly from ``values``."""
    model_config = ConfigDict(extra='forbid')
    type: Literal['choice']
    values: list[Any]

    @field_validator('values')
    @classmethod
    def _non_empty(cls, v: list[Any]) -> list[Any]:
        if not v:
            raise ValueError("choice dim requires a non-empty 'values' list")
        return v


# Typed search-space dimension; discriminated on the ``type`` tag so that a
# malformed domain is rejected by Pydantic rather than failing at sample time.
SearchDim = Annotated[Union[FloatDim, IntDim, ChoiceDim], Field(discriminator='type')]


class BudgetConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    batches: int = 1

    @field_validator('batches')
    @classmethod
    def _positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"search.budget.batches must be >= 1, got {v}")
        return v


class ExtractConfig(BaseModel):
    """The one scoring step: a plugin (entry-point name or ``path.py:Class``
    file ref relative to the ``.vast``) plus params passed to it."""
    model_config = ConfigDict(extra='forbid')
    plugin: str
    params: dict[str, Any] = {}


class ObjectiveSpec(BaseModel):
    """One optimized objective and its direction. ``name`` must match a key the
    extractor returns in ``ExtractResult.objectives``."""
    model_config = ConfigDict(extra='forbid')
    name: str
    direction: Literal['maximize', 'minimize'] = 'maximize'


class SearchConfig(BaseModel):
    """Closed-loop search over a typed parameter space.

    When present, execution runs as an iterative search: a strategy proposes
    parameter sets, an extractor scores them into objectives (+ measures), and
    the strategy is told the results to propose the next generation. Absent ⇒
    single batch (today's behaviour).

    Universal core (every strategy): ``strategy``, ``search_space``, ``extract``,
    ``objectives``, ``per_batch``, ``budget``, ``seed``, ``postprocessing``.
    Algorithm-specific tuning lives in ``strategy_parameters``, whose schema is
    owned and validated by the chosen strategy plugin (e.g. the QD archive).
    ``strategy``, ``extract.plugin`` and ``postprocessing`` entries may be
    entry-point names or local files relative to the ``.vast``.
    """
    model_config = ConfigDict(extra='forbid')
    strategy: str
    search_space: dict[str, SearchDim]
    extract: ExtractConfig
    objectives: list[ObjectiveSpec]
    per_batch: int
    budget: BudgetConfig = BudgetConfig()
    seed: Optional[int] = None
    # Postprocessing run over each batch's results before extract (e.g. to write
    # metrics.csv). Same format/loader as results_processing.postprocessing:
    # entry-point name, ``./path.py:Class`` file ref, or ``{name: {params}}``.
    postprocessing: Optional[list[Union[str, dict[str, Any]]]] = None
    # Free-form; validated by the strategy plugin's own params model at load.
    strategy_parameters: dict[str, Any] = {}

    @field_validator('search_space')
    @classmethod
    def _non_empty_space(cls, v: dict) -> dict:
        if not v:
            raise ValueError("search.search_space must declare at least one dimension")
        return v

    @field_validator('objectives')
    @classmethod
    def _non_empty_objectives(cls, v: list) -> list:
        if not v:
            raise ValueError("search.objectives must declare at least one objective")
        return v

    @field_validator('per_batch')
    @classmethod
    def _positive_per_batch(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"search.per_batch must be >= 1, got {v}")
        return v


class ConfigV1(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: int = 1
    metadata: Optional[dict[str, Any]] = None
    general: Optional[GeneralConfig] = None
    configuration: Optional[list[ConfigurationConfig]] = None
    execution: ExecutionConfig
    search: Optional[SearchConfig] = None
    results_processing: Optional[ResultsConfig] = None
    evaluation: Optional[EvaluationConfig] = None

    @model_validator(mode='after')
    def _search_xor_configuration(self):
        # Batch and search are mutually exclusive modes of the same `run`
        # command. A `search:` section synthesizes its configurations from
        # `search_space`, so it must not be paired with an explicit
        # `configuration:` block (whose entries may also carry `variations:`).
        if self.search is not None and self.configuration:
            raise ValueError(
                "'search' and 'configuration' are mutually exclusive: a search: "
                "section synthesizes its configurations from search_space, so the "
                "configuration: block (and its variations) must be empty/omitted.")
        return self


def validate_config(config: dict):
    """
    Validate the configuration settings.

    Args:
        settings: The settings dictionary to validate
    Raises:
        ValueError: If required sections are missing
    """
    logger.debug("Validating configuration")
    version = config.get("version", None)
    if version != 1:
        logger.error(f"Unsupported config version: {version}")
        raise ValueError(f"Unsupported config version: {version}")
    logger.debug(f"Config version {version} is supported")
    return get_validated_config(config, ConfigV1)


def get_validated_config(config: dict, config_class):
    try:
        logger.debug(f"Validating config against {config_class.__name__}")
        config = config_class(**config)
        logger.debug("Configuration validation successful")
    except Exception as e:
        if isinstance(e, ValidationError):
            errors = []
            for error in e.errors():  # pylint: disable=no-member
                field = ".".join(str(loc) for loc in error['loc'])
                msg = error['msg']
                errors.append(f"  - {field}: {msg}")
            error_msg = f"Config validation failed:\n" + "\n".join(errors)
            logger.error(error_msg)
            raise ValueError(error_msg) from None
        logger.error(f"Config validation failed: {str(e)}")
        raise ValueError(f"Config validation failed: {str(e)}") from None
    return config
