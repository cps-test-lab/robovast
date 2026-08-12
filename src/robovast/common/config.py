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
    name: str = Field(
        description="Unique scenario identifier, also used as the results "
        "directory name. Lowercase only; no underscores, spaces, or periods "
        "(e.g. 'nav2-controller-comparison').")
    parameters: Optional[list[ScenarioParameterConfig]] = None
    sim: Optional[dict] = Field(
        default=None,
        description="Fixed values for the simulator this configuration runs in, as a "
        "nested mapping against the backend's own schema (e.g. "
        "`{overrides: {plugins: {ceiling: {enabled: false}}}}`). The sibling of "
        "`parameters` for the other channel: `parameters` is what the trial does, `sim` "
        "is what it runs in. Merged over `execution.containers.simulation`, which stays "
        "the campaign-wide default.")
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


#: The container that runs scenario-execution. Always present; when a simulator backend
#: is declared it may be supplied by the backend rather than named by the campaign.
SCENARIO_CONTAINER = 'scenario'
#: The container the simulator runs in. May resolve to the same container as
#: :data:`SCENARIO_CONTAINER` (a stepped, in-process simulator) or to the same one as
#: :data:`SUT_CONTAINER` (a stack that bundles its own simulator).
SIMULATION_CONTAINER = 'simulation'
#: The system under test.
SUT_CONTAINER = 'sut'

#: The names with a defined meaning and a default image. Any other key in
#: ``execution.containers`` is an ad-hoc container and must state its own ``image``.
#:
#: These are *roles*, not a container count: one campaign may back all three with a
#: single container and another with three. Every tool that addresses a container --
#: ``exec_in_container``, a scenario's ``remote("ipc:///ipc/<name>")`` -- takes a name
#: from this same namespace, so a caller never has to know which.
CONTAINER_ROLES = (SCENARIO_CONTAINER, SIMULATION_CONTAINER, SUT_CONTAINER)


class ContainerConfig(BaseModel):
    """One container of a campaign: what it starts from, and what it adds.

    The same shape for every entry in ``execution.containers``, whether it is one of
    the known roles (:data:`CONTAINER_ROLES`) or an ad-hoc container.

    **One rule for ``image``: it is what the container *starts from*.** With no package
    keys that is also what it runs; with them, a derived image is built on top. There is
    no separate ``base_image`` and no author-chosen tag -- the tag is derived from the
    container's name, so a campaign states what a container *adds* and never what it
    adds to.
    """
    model_config = ConfigDict(extra='allow')

    #: Image to start from. Optional for a known role whose default comes from a
    #: simulator backend; required otherwise.
    image: Optional[str] = None
    #: apt packages installed into the image (``apt-get install -y``).
    system_packages: Optional[list[str]] = None
    #: Python packages installed into the image, as **install groups**. Same vocabulary
    #: as the top-level ``plugins:`` field -- an index pin (``shapely>=2.0``), a git URL
    #: (``pkg @ git+https://host/repo@ref``), or an uploaded workspace wheel
    #: (``./plugins/foo.whl``) -- PLUS a source directory relative to this ``.vast``
    #: (``packages/my_pkg``), which works here because the build copies the project dir
    #: into the image build context.
    #:
    #: Each element is either a spec (a group of one) or a **list** of specs installed
    #: together in one pip resolution pass, which is one image layer. If no element is a
    #: list the whole list is a single group -- the common case, and the one where order
    #: does not matter at all, because pip sees every local wheel at once and resolves an
    #: inter-package dependency against it instead of against PyPI.
    python_packages: Optional[list[Union[str, list[str]]]] = None
    #: What the container runs. Omitted for the roles RoboVAST drives itself (the
    #: scenario runner, a sidecar's scenario-execution server); required for an ad-hoc
    #: container, which nothing else knows how to start.
    command: Optional[list[str]] = None
    resources: Optional[ResourcesConfig] = None
    #: Simulator backend entry point (``simulation`` role only) -- a name in the
    #: ``robovast.simulators`` group, or a ``.vast``-relative ``<file>.py:<Class>`` ref.
    #: The backend's own keys ride alongside it and are validated by its CONFIG_CLASS.
    backend: Optional[str] = None

    @field_validator('system_packages')
    @classmethod
    def _validate_nonempty_strings(cls, v):
        if v is None:
            return v
        for entry in v:
            if not isinstance(entry, str) or not entry.strip():
                raise ValueError("each entry must be a non-empty string")
        return v

    @field_validator('python_packages')
    @classmethod
    def _validate_install_groups(cls, v):
        """Only what the ``str | list[str]`` annotation cannot say itself.

        Shape (a spec is a string, a group is a flat list of strings) is the
        annotation's job and pydantic rejects the rest before this runs; repeating it
        here would be two validators with one opinion. What is left is emptiness: a
        blank spec and an empty group both pass the type and mean nothing.
        """
        if v is None:
            return v
        for i, entry in enumerate(v):
            if isinstance(entry, str):
                if not entry.strip():
                    raise ValueError(f"entry {i} is blank; expected a package spec")
                continue
            if not entry:
                raise ValueError(
                    f"entry {i} is an empty install group; a group holds the specs "
                    "installed in one pip pass")
            for j, spec in enumerate(entry):
                if not spec.strip():
                    raise ValueError(
                        f"entry {i}, item {j} is blank; expected a package spec")
        return v

    def builds_image(self) -> bool:
        """Whether this container needs an image built on top of :attr:`image`."""
        return bool(self.system_packages or self.python_packages)


class ExecutionConfig(BaseModel):
    #: Every container this campaign runs, keyed by name -- the one namespace shared by
    #: the schema, ``exec_in_container`` and a scenario's ``remote()`` endpoints. Three
    #: names have a defined meaning (:data:`CONTAINER_ROLES`); anything else is an
    #: ad-hoc container. Replaces the former ``image`` / ``resources`` /
    #: ``secondary_containers`` / top-level ``build:``, which are gone in version 2.
    containers: dict[str, ContainerConfig]
    #: Campaign-wide, and reaches every container. There is deliberately no
    #: per-container ``env``: nothing needs one, and an injection is harmless where it
    #: is not read.
    env: Optional[list[dict[str, str]]] = None
    runs: int
    scenario_file: Optional[str] = None
    run_files: Optional[list[str]] = None
    #: Campaign inputs *derived* before composition, rather than authored next to the
    #: ``.vast`` — a map compiled from a floorplan, a browser scene descriptor compiled
    #: from a simulation world. Each entry is ``- <generator>: {out: <dir>, ...params}``
    #: (the single-key shorthand ``postprocessing`` uses), where ``out`` is a directory
    #: relative to the ``.vast``. Generation runs host-side *before* ``run_files`` are
    #: collected, and the produced files are appended to ``run_files`` — so they are
    #: content-hashed into the config identity, frozen into ``<campaign>/_config/`` and
    #: bind-mounted into the run exactly like a hand-written input. See
    #: :mod:`robovast.common.input_generation`.
    generate: Optional[list[Union[dict[str, Any], str]]] = None
    timeout: Optional[int] = None  # Maximum execution time in seconds per run
    # Simulation backend passed to scenario_execution as ``--simulation <module:Class>``.
    # Required by scenarios using wait_for_simulation_end() (e.g. MagBotSim).
    simulation: Optional[str] = None
    # Runner selection for scenario-execution inside the container, threaded to the entrypoint as
    # SCENARIO_MODE. ``auto`` (default) keeps the entrypoint's detection: use the ROS runner
    # (scenario_execution_ros) when ros2 is on PATH, else the non-ROS CLI. ``ros2`` forces the ROS
    # runner -- needed when a SimulationInterface must run alongside ROS behaviours (the ROS runner
    # ticks the SimulationInterface in its spin loop). ``base`` forces the non-ROS CLI
    # (scenario_execution) even when ros2 is on PATH -- for pure non-ROS scenarios (e.g. growth_sim).
    mode: str = "auto"
    # Record how the scenario's behaviour tree progressed, as ``behaviors.jsonl`` in the
    # run directory (threaded to the entrypoint as BT_LOG, becoming ``--bt-log``).
    # scenario_execution writes it directly, so unlike the rosbag route it replaced this
    # also works for ``mode: base`` runs. The ingest turns it into the ``behaviors``
    # table; the Run view's scenario-tree panel reads that table.
    #
    # On by default -- set it false only to opt a campaign *out*. A run whose tree state was
    # not recorded cannot be explained afterwards, and the file is small next to the rosbag.
    bt_log: bool = True
    # Topics the entrypoint's own recorder captures for the whole container's life, in WALL
    # time (threaded as LOG_TOPICS; ROS images only). Separate from the scenario's
    # ``bag_record``, which is sim-time and starts mid-run: this one sees the stack come up,
    # and ``/clock`` here is what lets postprocessing put a wall-stamped log line on the
    # playback clock.
    #
    # An escape hatch, not a switch: the default already covers what the ``run_log`` table
    # needs, and a campaign only names this to add a topic (or ``[]`` to record nothing).
    log_topics: list[str] = Field(default_factory=lambda: ["/rosout", "/clock"])
    # Job packing. ``runs_per_job`` is how many runs (a run = one configuration
    # at one run-number) are packed into a single job:
    #   1 (default): each job runs exactly one run. Right for simulators where
    #     setup dominates the execution time, one job == one scenario (e.g. Gazebo).
    #   >1: up to N runs are packed into one job and run sequentially inside a
    #     single simulator setup (the simulator is reset between them), amortising
    #     setup for simulators with cheap per-run cost. Runs are
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

    @field_validator('mode')
    @classmethod
    def validate_mode(cls, v: str) -> str:
        allowed = {"auto", "ros2", "base"}
        if v not in allowed:
            raise ValueError(f"execution.mode must be one of {sorted(allowed)}, got '{v}'")
        return v

    @field_validator('containers')
    @classmethod
    def validate_containers(cls, v: dict[str, 'ContainerConfig']) -> dict[str, 'ContainerConfig']:
        if not v:
            raise ValueError(
                "execution.containers must declare at least one container; the campaign "
                f"has nothing to run. The known roles are {', '.join(CONTAINER_ROLES)}.")
        for name, container in v.items():
            extras = set(container.model_extra or {})
            if name == SIMULATION_CONTAINER:
                # A backend's own keys (a world reference, say) ride alongside `backend`
                # and are validated against its CONFIG_CLASS once resolved.
                if extras and not container.backend:
                    raise ValueError(
                        f"execution.containers.{name} has unknown keys "
                        f"({', '.join(sorted(extras))}) and no 'backend' to validate them "
                        "against; a simulator backend owns its own keys.")
                continue
            if container.backend:
                raise ValueError(
                    f"execution.containers.{name} sets 'backend', which only the "
                    f"'{SIMULATION_CONTAINER}' container has.")
            if extras:
                raise ValueError(
                    f"execution.containers.{name} has unknown keys: "
                    f"{', '.join(sorted(extras))}")
            if name not in CONTAINER_ROLES and not container.image:
                raise ValueError(
                    f"execution.containers.{name} is an ad-hoc container and must state "
                    "an 'image'; only the known roles "
                    f"({', '.join(CONTAINER_ROLES)}) have a default.")
        return v

    # No schema check that the scenario container has an image: omitting it is
    # meaningful (build on / run the framework's own image, as an absent
    # ``build.base_image`` used to mean), and whether that resolves depends on
    # ``ROBOVAST_IMAGE``, which the schema cannot see. ``resolve_robovast_image`` makes
    # the call at run time and already refuses to fall back to a mutable default tag.
    # A *sidecar* is different -- it has no fallback at all -- and is checked above.

    @model_validator(mode='after')
    def _mode_auto_is_ambiguous_with_a_backend(self):
        """``auto`` resolves inside the container, too late for a backend to plan.

        The entrypoint picks the runner by testing whether ``ros2`` is on PATH, so the
        same ``.vast`` would get a different topology in a different image -- silently.
        A backend needs the answer at preparation time.
        """
        simulation = self.containers.get(SIMULATION_CONTAINER)
        if simulation is not None and simulation.backend and self.mode == "auto":
            raise ValueError(
                "execution.mode must be 'ros2' or 'base' when a simulator backend is "
                "declared: 'auto' is resolved inside the container by testing for ros2 "
                "on PATH, which would silently change the topology between images.")
        return self


#: Fallback wall-clock budget *per run* when ``execution.timeout`` is unset. One hour:
#: long enough for any scenario this substrate runs, short enough that a run which
#: never shuts itself down is eventually recognised as broken.
DEFAULT_RUN_DEADLINE_SECONDS = 60 * 60


def declared_per_run_seconds(execution_params: dict) -> Optional[int]:
    """The per-run budget the ``.vast`` **actually declared**, or ``None``.

    Distinct from :func:`per_run_deadline_seconds`, and the distinction matters
    because the two answer different questions:

    * *Enforcement* ("never let a campaign hang forever") may fall back to a
      backstop, because killing a wedged run an hour late still beats never.
    * *Reporting* ("is this run broken?") may **not**. Judging a two-minute pilot
      against an hour-long backstop yields ``stalled: false`` for the first hour of a
      run that is already dead — a health certificate for a corpse, which is worse
      than saying nothing. With no declared budget there is no honest threshold, so
      this returns ``None`` and the reader must decline to give a verdict.
    """
    timeout = (execution_params or {}).get("timeout")
    return int(timeout) if timeout else None


def per_run_deadline_seconds(execution_params: dict) -> int:
    """How long a single run may take before it is force-killed, in seconds.

    The **enforcement** figure: the cluster backend sets it as a Job
    ``activeDeadlineSeconds`` so a scenario that never shuts itself down cannot hang
    the campaign forever. Falls back to :data:`DEFAULT_RUN_DEADLINE_SECONDS`, which is
    why it must not be used to *report* health — see
    :func:`declared_per_run_seconds`.

    Only the cluster lane enforces this; locally nothing does (see ``execute_local``,
    which says so in the generated ``run.sh``).
    """
    return declared_per_run_seconds(execution_params) or DEFAULT_RUN_DEADLINE_SECONDS


class ResultsConfig(BaseModel):
    postprocessing: Optional[list[str | dict[str, Any]]] = None
    metadata_processing: Optional[list[str | dict[str, Any]]] = None
    publication: Optional[list[str | dict[str, Any]]] = None


class PlotSpec(BaseModel):
    """A user-declared eval plot: a read-only SQL query + a Vega-Lite encoding.

    The query runs over the campaign's ``data.db`` (``runs`` + metric tables,
    ``campaign.db`` attached); its result rows are bound into the Vega-Lite spec as
    ``data.values`` by the web eval viewer, so the spec declares only
    ``mark``/``encoding`` and its ``field`` names are the query's column aliases
    (no ``data`` block is authored).
    """
    title: str = ""
    query: str
    vega_lite: dict[str, Any] = {}


class EvaluationConfig(BaseModel):
    visualization: Optional[list[dict[str, Any]]] = None
    #: Declared plots, rendered by the web eval viewer (see :class:`PlotSpec`).
    plots: Optional[list[PlotSpec]] = None


#: Core built-in panel types bundled into the web UI itself (statically imported in
#: ``frontend/ui/src/panels``). Kept here (rather than only in the UI) so the ``.vast`` fails fast
#: on a typo instead of silently dropping a panel. Package-provided panels (e.g.
#: ``robovast_nav``'s ``costmap``) register in the ``robovast.panel_types`` entry-point
#: group and are accepted in addition to these (see ``PanelConfig._known_type``).
BUILTIN_PANEL_TYPES = frozenset({
    "playback", "scenario_tree", "scene", "scene3d", "timeseries", "state", "vega", "log",
    "camera",
})

#: Entry-point group for package-provided run-view panels (loaded as Module-Federation
#: remotes). Mirrors ``robovast.variation_types`` for variation-type web previews.
PANEL_TYPES_GROUP = "robovast.panel_types"

#: The panel type for a user-authored panel shipped as a built bundle next to the
#: ``.vast`` (referenced by its ``remote``/``module`` fields rather than by a registered
#: type name).
CUSTOM_PANEL_TYPE = "custom"

#: The panel type that renders an author-supplied Vega-Lite spec over a ``data.db`` table. Its
#: ``vega_lite``/``source`` bindings are validated by ``PanelConfig._vega_needs_bindings``.
VEGA_PANEL_TYPE = "vega"


class PanelPosition(BaseModel):
    """Where a panel sits in the run-view. ``anchor`` docks it against an edge, floats it
    at a corner or centred along an edge, or centres it; ``fill`` is used *instead of* an
    anchor and takes whatever space the docked panels leave over. ``width``/``height`` are
    pixels (int) or a percentage string like ``"40%"``. Omitted fields fall back to the
    panel type's registry default in the UI.

    A ``top``/``bottom`` bar reserves its height and a ``left``/``right`` column its width,
    so nothing else is laid out over them; everything else is placed in what is left."""
    model_config = ConfigDict(extra='forbid')
    anchor: Optional[Literal[
        'bottom', 'top', 'left', 'right',
        'top-left', 'top-right', 'bottom-left', 'bottom-right',
        # Centred along an edge and *floating above* that edge's reserved band rather than
        # docking into it, so e.g. ``bottom-center`` can share the bottom edge with the
        # playback bar (which owns the ``bottom`` dock). Give these a size; a full-width
        # ``bottom-center`` is just ``bottom``.
        'top-center', 'bottom-center', 'left-center', 'right-center',
        'center',
    ]] = None
    width: Optional[int | str] = None
    height: Optional[int | str] = None
    #: Occupy the space the docked panels leave over -- below/above the ``top``/``bottom``
    #: bars and beside the ``left``/``right`` columns -- instead of a declared
    #: ``width``/``height``. Used *instead of* an ``anchor``, not with one.
    fill: Optional[bool] = None

    @model_validator(mode='after')
    def _placement_is_unambiguous(self):
        # Every combination below would be silently ignored by the layout engine, which is
        # how a panel ends up somewhere its author did not ask for and cannot explain.
        if self.fill:
            if self.anchor is not None:
                raise ValueError(
                    f"position.fill takes the space the docked panels leave over, so it "
                    f"replaces the anchor -- drop one of the two (got anchor: '{self.anchor}')."
                )
            if self.width is not None or self.height is not None:
                raise ValueError(
                    "position.fill sizes the panel from the space left over; a width/height "
                    "on it would be ignored. Drop them, or anchor the panel instead."
                )
        if self.anchor in ('top', 'bottom') and self.width is not None:
            raise ValueError(
                f"a '{self.anchor}' bar spans the full width, so its width is ignored. Drop "
                f"it, or use '{self.anchor}-center'/a corner to place a narrower panel."
            )
        return self


class PanelConfig(BaseModel):
    """One panel of the web run-view. ``type`` selects the panel plugin; the panel's
    own data bindings (e.g. ``layers``/``source`` naming ``data.db`` tables) are carried
    as extra keys (``extra='allow'``) and interpreted by that plugin.

    ``type`` is one of the core built-ins (:data:`BUILTIN_PANEL_TYPES`), a package-provided
    panel registered in the :data:`PANEL_TYPES_GROUP` entry-point group, or
    :data:`CUSTOM_PANEL_TYPE` for a user-authored panel shipped as a built bundle next to
    the ``.vast``. A ``custom`` panel names its bundle via ``remote`` (a path relative to
    the ``.vast`` directory, to the bundle dir or its ``remoteEntry.js``) and ``module``
    (the exposed Module-Federation module, default ``./panel``)."""
    model_config = ConfigDict(extra='allow')
    type: str
    title: Optional[str] = None
    position: Optional[PanelPosition] = None
    #: Whether the panel's free edge/corner can be dragged to resize it in the run-view.
    #: Defaults to on for every panel type that does not turn it off (the docked playback
    #: bar, the full-view 3D background).
    resizable: Optional[bool] = None
    minimizable: Optional[bool] = None
    minimized: Optional[bool] = None
    hidden: Optional[bool] = None
    #: Lock the panel's geometry: the run-view lets a panel be dragged by its title bar and
    #: resized by its free edge, and ``fixed: true`` opts this one out of both.
    fixed: Optional[bool] = None
    #: For ``type: custom`` -- path (relative to the ``.vast``) to the built panel bundle.
    remote: Optional[str] = None
    #: For ``type: custom`` -- the exposed Module-Federation module to render.
    module: Optional[str] = None

    @model_validator(mode='before')
    @classmethod
    def _flatten_shorthand(cls, v):
        # The .vast authors panels in shorthand -- ``- playback:`` / ``- costmap: {...}`` (a
        # single-key mapping keyed by the type) or a bare ``- playback`` string -- which the
        # service flattens to ``{type, ...fields}`` for the UI (see list_campaign_panels).
        # Accept the same shorthand here so validation matches what the runtime serves.
        if isinstance(v, str):
            return {'type': v}
        if isinstance(v, dict) and 'type' not in v and len(v) == 1:
            (ptype, props), = v.items()
            if props is None or isinstance(props, dict):
                return {'type': ptype, **(props or {})}
        return v

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema, handler):
        # ``_flatten_shorthand`` accepts shapes the post-validation model can't express:
        # a bare string (the panel type), or a single-key mapping keyed by the type
        # (``- playback:`` / ``- costmap: {title: ...}``) instead of a ``type`` property.
        # The default schema would require a literal ``type`` property, so the web editor
        # flags valid YAML as ``Missing property "type"``. Reflect the real accepted forms
        # here; the shorthand branch keeps the remaining panel properties so the editor
        # still completes/validates ``title``, ``position`` & co. inside it.
        default = handler(core_schema)
        props = {k: v for k, v in default.get('properties', {}).items() if k != 'type'}
        return {
            'anyOf': [
                {'type': 'string'},
                default,
                {
                    'type': 'object',
                    'minProperties': 1,
                    'maxProperties': 1,
                    'additionalProperties': {
                        'anyOf': [
                            {'type': 'null'},
                            {'type': 'object', 'properties': props, 'additionalProperties': True},
                        ],
                    },
                },
            ],
        }

    @field_validator('type')
    @classmethod
    def _known_type(cls, v):
        # Valid types: core built-ins + installed package panels (entry points) + ``custom``.
        # Package types come from the entry-point group so validation matches what the runtime
        # can actually serve; e.g. ``costmap`` is valid only when ``robovast_nav`` is installed.
        from robovast.common.plugin_ref import \
            list_ref_names  # pylint: disable=import-outside-toplevel
        allowed = BUILTIN_PANEL_TYPES | list_ref_names(PANEL_TYPES_GROUP) | {CUSTOM_PANEL_TYPE}
        if v not in allowed:
            raise ValueError(
                f"unknown panel type {v!r}; expected one of {', '.join(sorted(allowed))} "
                f"(package panels require the providing plugin, e.g. 'robovast_nav', installed)")
        return v

    @model_validator(mode='after')
    def _custom_needs_remote(self):
        # A ``custom`` panel is referenced by its bundle location, not a registered name.
        if self.type == CUSTOM_PANEL_TYPE and not self.remote:
            raise ValueError(
                "a 'custom' panel must set 'remote' (path to its built bundle relative to "
                "the .vast); 'module' is optional (default './panel')")
        # ``remote``/``module`` only mean something for a custom panel.
        if self.type != CUSTOM_PANEL_TYPE and (self.remote or self.module):
            raise ValueError(
                f"'remote'/'module' are only valid on a 'custom' panel, not {self.type!r}")
        return self

    @model_validator(mode='after')
    def _vega_needs_bindings(self):
        # A ``vega`` panel's bindings are passthrough extras like every other panel's, so they are
        # checked here rather than declared as fields (which would put one panel type's schema on
        # the model every panel shares). Checked at the *model* level and not only in
        # ``config_validation._panel_problems`` because campaign generation goes through
        # ``validate_config`` alone -- otherwise a malformed panel first surfaces in the browser at
        # replay time, long after the compute was spent.
        if self.type != VEGA_PANEL_TYPE:
            return self
        extra = self.__pydantic_extra__ or {}
        spec = extra.get('vega_lite')
        if not isinstance(spec, dict) or not spec:
            raise ValueError(
                "a 'vega' panel must set 'vega_lite' to a non-empty Vega-Lite spec "
                "(mark/encoding, or layer/vconcat); the panel binds the rows as its data, so the "
                "spec declares no 'data' block")
        source = extra.get('source')
        if not isinstance(source, dict) or not source.get('table'):
            raise ValueError(
                "a 'vega' panel must set 'source' to a data.db table, e.g. "
                "source: {table: poses, filter: {frame: base_link}}")
        return self


class TimelineConfig(BaseModel):
    """Which ``data.db`` table + column defines the run's playback timeline. Set this
    for non-ROS runs whose time lives in a table other than the nav defaults (e.g. a
    sim's ``trajectory`` table with a ``t`` column); when omitted the UI derives the
    range from the standard ``poses``/``behaviors``/``scenario_timestamps`` tables."""
    model_config = ConfigDict(extra='forbid')
    table: str
    time_column: str = 'timestamp'


class VisualizationConfig(BaseModel):
    """The web run-view: an ordered list of panels for replaying a single run of a
    postprocessed campaign over its timeline. Rendered by the UI from the campaign's
    snapshot ``.vast``; each panel reads existing ``data.db`` tables."""
    model_config = ConfigDict(extra='forbid')
    timeline: Optional[TimelineConfig] = None
    panels: Optional[list[PanelConfig]] = Field(default_factory=list)

    @field_validator('panels', mode='before')
    @classmethod
    def _default_empty(cls, v):
        # ``panels:`` with no value parses as YAML null; treat it as an empty list
        # (Optional keeps the served JSON Schema from flagging null inline too).
        return [] if v is None else v

    @model_validator(mode='after')
    def _column_members_sized(self):
        # A ``left``/``right`` column holds a stack: its members tile down one gutter, and one
        # without a ``height`` takes the rest of it -- so anything declared after that member on
        # the same side would be laid out on top of it. Bars are exempt: an undeclared bar height
        # falls back to the panel's default rather than meaning "the rest".
        for side in ('left', 'right'):
            members = [p for p in (self.panels or [])
                       if p.position and p.position.anchor == side and not p.hidden]
            for p in members[:-1]:
                if p.position.height is None:
                    raise ValueError(
                        f"panel '{p.type}' is in the '{side}' column with no height, so it takes "
                        f"the rest of the column and the {len(members) - members.index(p) - 1} "
                        f"panel(s) after it would land on top of it. Give it a height, or make it "
                        f"the last '{side}' panel."
                    )
        return self

    @model_validator(mode='after')
    def _one_fill_panel(self):
        # Two filling panels occupy the same rectangle at the same depth, so one of them is
        # simply invisible -- a layout mistake worth naming rather than rendering.
        filling = [p for p in (self.panels or [])
                   if p.position and p.position.fill and not p.hidden]
        if len(filling) > 1:
            raise ValueError(
                f"{len(filling)} panels declare position.fill "
                f"({', '.join(p.type for p in filling)}); they would occupy the same "
                f"rectangle. Keep one and anchor or hide the rest."
            )
        return self


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
    plugins: Optional[list[str]] = Field(
        default=None,
        description=(
            "Python plugin packages this campaign needs, as pip requirement specs — "
            "for both third-party variation types (not built into robovast/robovast-"
            "nav) and postprocessing plugins (an entry-point postprocessing command, "
            "or the third-party dependencies a local './file.py:Class' postprocessing "
            "plugin imports). Each entry is one of: an index pin ('my_plugin==1.2.3'); "
            "a git URL ('scenario_mt @ git+https://github.com/org/repo@ref' — for a "
            "private repo, provide a GitHub token at 'vast exec cluster setup'); or a "
            "workspace-relative path to a wheel you uploaded "
            "('./plugins/my_plugin-1.0-py3-none-any.whl'). They are installed into the "
            "'.robovast_plugins/' dir (with dependencies) and put on sys.path before "
            "composing (so variation names resolve) and before postprocessing (so "
            "postprocessing plugins and their deps resolve, including on a re-run)."),
    )
    configuration: Optional[list[ConfigurationConfig]] = None
    execution: ExecutionConfig
    search: Optional[SearchConfig] = None
    results_processing: Optional[ResultsConfig] = None
    evaluation: Optional[EvaluationConfig] = None
    #: The web run-view panels (see :class:`VisualizationConfig`). Top-level (distinct
    #: from ``evaluation.visualization``, which drives the notebook analysis views).
    visualization: Optional[VisualizationConfig] = None

    @field_validator('plugins')
    @classmethod
    def _validate_plugins(cls, v):
        # Each entry is a pip requirement spec (a git URL such as
        # ``pkg @ git+https://host/repo@ref`` or an index pin ``pkg==1.2.3``)
        # installed into the composing environment before variation resolution.
        # A bare local path won't resolve inside the controller pod, so entries
        # should be network-reachable; we only enforce non-empty strings here.
        if v is None:
            return v
        for entry in v:
            if not isinstance(entry, str) or not entry.strip():
                raise ValueError("each 'plugins' entry must be a non-empty string "
                                 "(a pip requirement spec, e.g. a git+https URL)")
        return v

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


#: What a version 1 config has to become. There is no migration tool and no v1 reader,
#: so this message *is* the migration path -- it has to be instructions, not a
#: complaint. Keep each entry as "what you wrote" -> "what it becomes".
_V1_MIGRATION = (
    "  execution.image: <img>          ->  execution.containers.scenario.image: <img>\n"
    "  execution.resources: {...}      ->  execution.containers.scenario.resources: {...}\n"
    "  execution.secondary_containers: ->  one execution.containers.<name> entry each\n"
    "    - nav: {resources: {...}}     ->    nav: {image: <img>, command: [...], resources: {...}}\n"
    "  build: {base_image: <img>,      ->  execution.containers.<role>:\n"
    "          system_packages: [...],       image: <img>            # what it starts FROM\n"
    "          python_packages: [...],       system_packages: [...]\n"
    "          tag: <tag>}                   python_packages: [...]\n"
    "                                  (no 'tag' and no 'base_image': the tag comes from the\n"
    "                                   container name, and 'image' IS the base)\n"
    "\n"
    "The known container roles are 'scenario' (runs scenario-execution), 'simulation' "
    "(the simulator) and 'sut' (the system under test); any other name is an ad-hoc "
    "container and must state its own image and command.")


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
    if version == 1:
        # Version 1 is not read at all -- there is no dual-parse path. Say what moved
        # where, because nothing else will: a campaign's archived _config/*.vast stays
        # v1 forever and simply stops being re-readable.
        raise ValueError(
            "config version 1 is no longer supported. Version 2 replaces "
            "execution.image, execution.resources, execution.secondary_containers and "
            "the top-level build: section with a single execution.containers mapping.\n"
            "\n" + _V1_MIGRATION + "\n\n"
            "Then set 'version: 2'.")
    if version != 2:
        # Raised, not logged-and-raised: every caller reports the failure it catches,
        # so logging the same text here printed it twice.
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
            error_msg = "Config validation failed:\n" + "\n".join(errors)
            raise ValueError(error_msg) from None
        raise ValueError(f"Config validation failed: {str(e)}") from None
    return config
