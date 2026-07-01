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
import re
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import (BaseModel, ConfigDict, Field, ValidationError,
                      field_validator, model_validator)

logger = logging.getLogger(__name__)

# A search-variable marker: a string whose *entire* value is ``$name`` or
# ``${name}``. Only a standalone token is a reference (no mid-string interp), so
# the substituted value keeps its native type. Disjoint from the ``@name``
# scenario-parameter reference resolved inside variation plugins.
_VAR_RE = re.compile(r'^\$(?:\{([A-Za-z_]\w*)\}|([A-Za-z_]\w*))$')


def match_var_marker(value: Any) -> Optional[str]:
    """Return the referenced variable name if ``value`` is a ``$name``/``${name}``
    marker string, else ``None``. A leading ``$$`` is an escaped literal ``$``."""
    if not isinstance(value, str):
        return None
    m = _VAR_RE.match(value)
    if not m:
        return None
    return m.group(1) or m.group(2)


def _collect_var_refs(node: Any, refs: set) -> None:
    """Walk plain data (dicts/lists/scalars) collecting every ``$name`` marker."""
    if isinstance(node, dict):
        for v in node.values():
            _collect_var_refs(v, refs)
    elif isinstance(node, (list, tuple)):
        for v in node:
            _collect_var_refs(v, refs)
    else:
        name = match_var_marker(node)
        if name is not None:
            refs.add(name)


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
    # Simulation backend passed to scenario_execution as ``--simulation <module:Class>``.
    # Required by scenarios using wait_for_simulation_end() (e.g. MagBotSim).
    simulation: Optional[str] = None
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


class BoolDim(BaseModel):
    """A boolean search dimension — sugar for a two-value categorical."""
    model_config = ConfigDict(extra='forbid')
    type: Literal['bool']


# Typed search-space dimension; discriminated on the ``type`` tag so that a
# malformed domain is rejected by Pydantic rather than failing at sample time.
SearchDim = Annotated[Union[FloatDim, IntDim, ChoiceDim, BoolDim],
                      Field(discriminator='type')]


class BatchesBudget(BaseModel):
    """Resource cap: stop after this many ask/tell batches."""
    model_config = ConfigDict(extra='forbid')
    type: Literal['batches']
    value: int

    @field_validator('value')
    @classmethod
    def _positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"budget batches value must be >= 1, got {v}")
        return v


class TimeBudget(BaseModel):
    """Resource cap: stop after this many seconds of wall-clock time."""
    model_config = ConfigDict(extra='forbid')
    type: Literal['time']
    seconds: float

    @field_validator('seconds')
    @classmethod
    def _positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(f"budget time seconds must be > 0, got {v}")
        return v


# A resource cap; the search stops when ANY budget criterion is hit.
BudgetCriterion = Annotated[
    Union[BatchesBudget, TimeBudget],
    Field(discriminator='type')]


class TargetObjectiveStop(BaseModel):
    """Stop when the best objective reaches ``value`` (direction-aware)."""
    model_config = ConfigDict(extra='forbid')
    type: Literal['target_objective']
    value: float


class NoImprovementStop(BaseModel):
    """Stop when the best objective has not improved by >= ``min_delta`` for
    ``patience`` consecutive batches (early-stopping / convergence)."""
    model_config = ConfigDict(extra='forbid')
    type: Literal['no_improvement']
    patience: int
    min_delta: float = 0.0

    @field_validator('patience')
    @classmethod
    def _positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"no_improvement.patience must be >= 1, got {v}")
        return v


class MetricStop(BaseModel):
    """Stop when a strategy-reported metric (``report().extra[name]``, e.g. the
    QD ``coverage`` / ``qd_score``) satisfies ``op value``."""
    model_config = ConfigDict(extra='forbid')
    type: Literal['metric']
    name: str
    op: Literal['>=', '<=', '>', '<'] = '>='
    value: float


# A convergence / quality early-exit; the search stops when ANY fires (resource
# caps live in the parallel ``budget`` list).
StopCriterion = Annotated[
    Union[TargetObjectiveStop, NoImprovementStop, MetricStop],
    Field(discriminator='type')]


# Each budget/stopping entry is written as a single-key mapping (like variations):
# ``- batches: 200`` or ``- metric: {name: coverage, op: '>=', value: 0.3}``. The
# key is the criterion name; a scalar value is shorthand for the field named below
# (criteria with several required fields must use a mapping). The ``type``
# discriminator is injected from the key so the unions above still validate.
_BUDGET_SCALAR = {'batches': 'value', 'time': 'seconds'}
_STOPPING_SCALAR = {'target_objective': 'value', 'no_improvement': 'patience'}


def _normalize_criterion(entry: Any, scalar_fields: dict, kind: str) -> dict:
    if not isinstance(entry, dict) or len(entry) != 1:
        raise ValueError(
            f"each search.{kind} entry must be a single-key mapping, e.g. "
            f"'- batches: 200' or '- metric: {{name: coverage, value: 0.3}}'; got {entry!r}")
    key, val = next(iter(entry.items()))
    if isinstance(val, dict):
        return {'type': key, **val}
    if key not in scalar_fields:
        raise ValueError(
            f"search.{kind} '{key}' needs a mapping of parameters, not a scalar")
    return {'type': key, scalar_fields[key]: val}


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
    # Resource caps and convergence early-exits: two parallel typed-criteria
    # lists, all OR-combined and evaluated by the controller after each batch. At
    # least one criterion across the two is required (a search needs a way to end).
    budget: Optional[list[BudgetCriterion]] = None
    stopping: Optional[list[StopCriterion]] = None
    seed: Optional[int] = None
    # Optional variation template + fixed scenario params, identical in shape to a
    # batch ``configuration:`` block. The template fixes most variation params and
    # references searched ones with a ``$name`` / ``${name}`` marker resolving to a
    # search_space dimension; Compose substitutes per proposed parameter set. Any
    # search_space dim not referenced here falls back to a direct scenario param.
    # Kept as raw mappings (not VariationConfig/ScenarioParameterConfig, which drop
    # unknown keys) so the marker references survive for the validator below and
    # the substitution in Compose; the plugin params are validated at generation.
    variations: Optional[list[dict[str, Any]]] = None
    parameters: Optional[list[dict[str, Any]]] = None
    # Postprocessing run over each batch's results before extract (e.g. to write
    # metrics.csv). Same format/loader as results_processing.postprocessing:
    # entry-point name, ``./path.py:Class`` file ref, or ``{name: {params}}``.
    postprocessing: Optional[list[Union[str, dict[str, Any]]]] = None
    # Free-form; validated by the strategy plugin's own params model at load.
    strategy_parameters: dict[str, Any] = {}

    @field_validator('budget', mode='before')
    @classmethod
    def _norm_budget(cls, v):
        if v is None:
            return v
        if not isinstance(v, list):
            raise ValueError("search.budget must be a list of single-key mappings")
        return [_normalize_criterion(e, _BUDGET_SCALAR, 'budget') for e in v]

    @field_validator('stopping', mode='before')
    @classmethod
    def _norm_stopping(cls, v):
        if v is None:
            return v
        if not isinstance(v, list):
            raise ValueError("search.stopping must be a list of single-key mappings")
        return [_normalize_criterion(e, _STOPPING_SCALAR, 'stopping') for e in v]

    @field_validator('search_space')
    @classmethod
    def _non_empty_space(cls, v: dict) -> dict:
        if not v:
            raise ValueError("search.search_space must declare at least one dimension")
        return v

    @model_validator(mode='after')
    def _validate_var_references(self):
        # Every ``$name`` / ``${name}`` marker in the variations/parameters
        # template must resolve to a declared search_space dimension. This is a
        # pure string/tree walk on plain data — it must NOT instantiate variation
        # CONFIG_CLASS models (they would reject the marker strings).
        declared = set(self.search_space)
        refs: set[str] = set()
        for tmpl in (self.variations, self.parameters):
            if tmpl is not None:
                _collect_var_refs(tmpl, refs)
        unknown = sorted(refs - declared)
        if unknown:
            raise ValueError(
                f"search.variations/parameters reference unknown search_space "
                f"variable(s) {unknown}; declared dimensions: {sorted(declared)}")
        return self

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

    @model_validator(mode='after')
    def _validate_stopping(self):
        # A search needs at least one way to end: a budget cap or a stopping
        # criterion. Without either it would run forever.
        if not self.budget and not self.stopping:
            raise ValueError(
                "a search must define at least one 'budget' or 'stopping' "
                "criterion (e.g. budget: [{type: batches, value: 20}])")
        # target_objective / no_improvement compare the single objective, so they
        # require a single-objective search (matches SearchStrategy.single_objective).
        single_only = {'target_objective', 'no_improvement'}
        for crit in (self.stopping or []):
            if crit.type in single_only and len(self.objectives) != 1:
                raise ValueError(
                    f"search.stopping '{crit.type}' requires a single objective, "
                    f"but {len(self.objectives)} are configured")
        return self


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
