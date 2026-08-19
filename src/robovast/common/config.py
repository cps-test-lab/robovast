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
import math
import re
from functools import lru_cache
from typing import Annotated, Any, ClassVar, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from robovast.common.quantity import to_cores

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

    ``cpu`` takes fractional cores (``0.5``) and the millicore spelling Kubernetes uses
    (``"500m"``), not only whole cores. On the cluster lane a campaign's throughput is
    ``quota // pod_request``, so rounding a measured 0.3-core sidecar up to a whole core is
    paid on **every job of the sweep** — and both lanes have always accepted the fractional
    value (the Kubernetes manifest takes ``str(cpu)``, Compose takes ``cpus: '<cpu>'``). The
    integer-only annotation was the only thing rejecting it.
    """
    # ``int`` first so a whole-core declaration stays an int: the lanes render the value with
    # ``str()``, and coercing 4 to 4.0 would rewrite every existing campaign's manifest from
    # "4" to "4.0" for no reason.
    cpu: Optional[Union[int, float, str, list[dict[str, Union[int, float, str]]]]] = None
    memory: Optional[Union[str, list[dict[str, str]]]] = None
    #: Whole GPUs for this container. Omit it and the container running the simulator gets
    #: one wherever the cluster advertises GPUs, so the common case needs nothing here;
    #: ``0`` opts out on a cluster that has them. A real field rather than an undeclared key
    #: because pydantic's default ``extra='ignore'`` was dropping it from the model, so the
    #: documented option only worked where a lane happened to read the raw mapping.
    gpu: Optional[Union[int, list[dict[str, int]]]] = None

    @field_validator('cpu')
    @classmethod
    def validate_cpu_quantity(cls, v):
        """Reject a cpu value that is not a CPU quantity.

        Without this, ``str`` in the annotation would accept ``"4Gi"`` or ``"lots"`` and pass
        it through to the manifest, where the failure surfaces as a pod that never schedules
        — far from the line that caused it.
        """
        def check(value):
            if to_cores(value) is None:
                raise ValueError(
                    f'cpu {value!r} is not a CPU quantity: use cores (4, 0.5) '
                    'or millicores ("500m")')

        if v is None:
            return v
        if isinstance(v, list):
            for entry in v:
                for value in entry.values():
                    check(value)
        else:
            check(v)
        return v


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


class ImageProvenanceConfig(BaseModel):
    """Where a user-supplied image came from, stated by the author.

    Only meaningful on a container whose ``image`` robovast did not build. For anything robovast
    builds, or any member of the published family, the provenance is known by construction and
    this block is redundant -- declaring it there would invite it to drift from the truth.

    It exists because that one field otherwise makes a whole campaign untraceable: nothing in the
    results can say what the image was, and the gap surfaces a year later when it is too late to
    ask. robovast cannot derive this, so the author is the only source.

    **Not to be confused with the generated provenance** in ``_execution/``. That records what
    robovast observed; this records what only the author knows. Which is also why it lives in the
    ``.vast``: it is intent, and intent belongs with the rest of the intent.
    """
    model_config = ConfigDict(extra='forbid')

    #: Where the image's build definition lives -- a repository URL, or a path inside a repo.
    source: str
    #: The commit that built it. A tag or branch is accepted but is not a pin, and a re-run a
    #: year from now will say so rather than pretending the ref still means the same thing.
    revision: str
    #: How it was built, for a human who has to reproduce it. Optional, because a repo at a
    #: commit is often enough; useful when the build is not the obvious one.
    build_recipe: Optional[str] = None


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
    #: Where a user-supplied ``image`` came from. Required when the image is one robovast
    #: neither built nor publishes, because otherwise the campaign records nothing that could
    #: identify what it ran -- see :class:`ImageProvenanceConfig`.
    #:
    #: Declared rather than left to ``extra='allow'``: an undeclared key is accepted silently
    #: here, so a typo like ``provenence:`` would look exactly like compliance while providing
    #: nothing.
    provenance: Optional[ImageProvenanceConfig] = None
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

    # No schema check that the scenario container has an image: omitting it is the normal
    # case, not an omission. It means "the RoboVAST framework image", whose project and
    # tag are the deployment's to choose and are not visible to a schema.
    # A *sidecar* is different -- it has no such default at all, because inventing an
    # image for the thing under test would run something nobody named -- and is checked
    # above.

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
#: never shuts itself down is eventually recognized as broken.
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


#: Core built-in run-view panel types bundled into the web UI itself (statically imported in
#: ``frontend/ui/src/panels/run_view``). Kept here (rather than only in the UI) so the ``.vast``
#: fails fast on a typo instead of silently dropping a panel. Package-provided panels (e.g.
#: ``robovast_nav``'s ``costmap``) register in the ``robovast.panel_types`` entry-point
#: group and are accepted in addition to these (see ``RunViewPanelConfig._known_type``).
BUILTIN_PANEL_TYPES = frozenset({
    "playback", "scenario_tree", "scene", "scene3d", "timeseries", "state", "vega", "log",
    "camera",
})

#: Core built-in *config-view* panel types (``frontend/ui/src/panels/config``). A second
#: vocabulary rather than a second mechanism: the two surfaces take different props (a config
#: panel gets a resolved configuration, a run panel a playback clock and a run-scoped data
#: provider), so a run panel declared in ``visualization.config.panels`` -- or the reverse --
#: has to be refused rather than mounted against props it cannot read.
BUILTIN_CONFIG_PANEL_TYPES = frozenset({
    "parameters", "world", "scene3d",
})

#: Entry-point group for package-provided panels of *either* surface (loaded as
#: Module-Federation remotes). Mirrors ``robovast.variation_types`` for variation-type web
#: previews. Which surface a registered panel belongs to is the panel class's own ``SURFACE``
#: attribute (``"run"`` by default, ``"config"`` for a config-view panel), so one group, one
#: web-asset attribute and one asset route serve both.
PANEL_TYPES_GROUP = "robovast.panel_types"

#: The surface a panel type belongs to, when its class does not say. ``run`` because every
#: panel that existed before the config view is one, so an unmarked third-party panel keeps
#: working unchanged.
DEFAULT_PANEL_SURFACE = "run"

#: The other surface: the Config tab's third column. Named rather than spelled ``"config"`` at each
#: use, because three separate things key on it -- the type check, the served schema and the panel
#: plugins' own ``SURFACE`` -- and a typo in one of them reads as "no such panel type".
CONFIG_PANEL_SURFACE = "config"

#: How each surface is named in a message, and the ``.vast`` block a panel of it is declared in.
#: Two mappings rather than string arithmetic at each site, because both halves are read by the
#: "wrong surface" diagnosis, which exists to point an author at the right block.
_PANEL_SURFACE_LABELS = {DEFAULT_PANEL_SURFACE: "run-view", CONFIG_PANEL_SURFACE: "config-view"}
_PANEL_SURFACE_BLOCKS = {
    DEFAULT_PANEL_SURFACE: "visualization.results.run_view.panels",
    CONFIG_PANEL_SURFACE: "visualization.config.panels",
}


@lru_cache(maxsize=None)
def panel_type_names(surface: str) -> frozenset:
    """Names of the installed :data:`PANEL_TYPES_GROUP` panels belonging to *surface*.

    Needs the classes themselves -- ``SURFACE`` is a class attribute -- so unlike
    :func:`~robovast.common.plugin_ref.list_ref_names` this imports each plugin. Best-effort
    in the same way: a plugin that fails to import is attributed to **both** surfaces rather
    than dropped, because "unknown panel type" would be a wrong diagnosis of a broken import,
    and the runtime reports the real failure when it tries to serve the bundle.
    """
    from importlib.metadata import entry_points  # pylint: disable=import-outside-toplevel
    names = set()
    try:
        eps = list(entry_points().select(group=PANEL_TYPES_GROUP))
    except Exception:  # noqa: BLE001 - enumeration must never break validation
        logger.debug("could not enumerate entry-point group %r", PANEL_TYPES_GROUP)
        return frozenset()
    for ep in eps:
        try:
            if getattr(ep.load(), "SURFACE", DEFAULT_PANEL_SURFACE) == surface:
                names.add(ep.name)
        except Exception:  # noqa: BLE001 - a broken plugin is not an unknown panel type
            logger.debug("panel plugin %r failed to load; accepting it on every surface", ep.name)
            names.add(ep.name)
    return frozenset(names)


@lru_cache(maxsize=None)
def panel_type_bindings(surface: str) -> dict:
    """``{panel type: bindings model}`` for the installed panels of *surface* that declare one.

    A panel type declares its accepted bindings as ``CONFIG_CLASS``, the same attribute a variation
    type uses -- which is why ``get_plugin_details`` describes a panel's fields without knowing
    anything about panels. Types that declare none are absent, and keep the free-form ``extra`` rule:
    the run view's ``vega_lite``/``layers``/``series`` bindings are rich and unmodelled, and none of
    them has to be modelled for a panel that *does* declare its fields to be checked.

    Best-effort exactly as :func:`panel_type_names` is: a plugin that fails to import contributes no
    model, so its bindings stay unvalidated rather than reading as "unknown field".
    """
    from importlib.metadata import entry_points  # pylint: disable=import-outside-toplevel
    models = {}
    try:
        eps = list(entry_points().select(group=PANEL_TYPES_GROUP))
    except Exception:  # noqa: BLE001 - enumeration must never break validation
        logger.debug("could not enumerate entry-point group %r", PANEL_TYPES_GROUP)
        return models
    for ep in eps:
        try:
            cls = ep.load()
            if getattr(cls, "SURFACE", DEFAULT_PANEL_SURFACE) != surface:
                continue
            model = getattr(cls, "CONFIG_CLASS", None)
            if isinstance(model, type) and issubclass(model, BaseModel):
                models[ep.name] = model
        except Exception:  # noqa: BLE001 - a broken plugin is not a broken binding
            logger.debug("panel plugin %r failed to load; its bindings stay unchecked", ep.name)
    return models


def bindings_model_for(panel_type: str, surface: str):
    """The bindings model a panel type declares, or ``None`` when it declares none."""
    from robovast.common.panel_bindings import \
        BUILTIN_PANEL_BINDINGS  # pylint: disable=import-outside-toplevel
    if surface == CONFIG_PANEL_SURFACE and panel_type in BUILTIN_PANEL_BINDINGS:
        return BUILTIN_PANEL_BINDINGS[panel_type]
    return panel_type_bindings(surface).get(panel_type)


def visualization_block(cfg, *path):
    """Walk ``visualization.<path...>`` of a **raw** config dict; ``None`` if any step is absent.

    Several readers take a snapshot ``.vast`` raw rather than through :class:`ConfigV1` -- reading
    a campaign's declared panels or plots must not depend on the rest of its config still being
    re-validatable. Without this each of them spells the nested path out as a chain of
    ``(… or {}).get(…)``, which is where a key rename goes wrong quietly.
    """
    node = cfg or {}
    for key in ('visualization',) + path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
        if node is None:
            return None
    return node


def flatten_panel_shorthand(v):
    """Accept the ``.vast``'s panel shorthand and return ``{type, ...fields}``.

    A panel is authored as a single-key mapping keyed by its type (``- playback:`` /
    ``- costmap: {title: ...}``) or as a bare string (``- playback``), and the service
    flattens it the same way before serving it to the UI (see ``list_campaign_panels``).
    Shared by both panel surfaces so validation cannot come to accept a shape the runtime
    does not, or the other way round.
    """
    if isinstance(v, str):
        return {'type': v}
    if isinstance(v, dict) and 'type' not in v and len(v) == 1:
        (ptype, props), = v.items()
        if props is None or isinstance(props, dict):
            return {'type': ptype, **(props or {})}
    return v


def panel_json_schema(core_schema, handler, surface: str = DEFAULT_PANEL_SURFACE,
                      builtins: frozenset = frozenset()):
    """JSON Schema for a panel, reflecting the shorthand :func:`flatten_panel_shorthand` takes.

    The shorthand accepts shapes the post-validation model cannot express: a bare string (the
    type), or a single-key mapping keyed by the type instead of a ``type`` property. The default
    schema would require a literal ``type``, so the web editor flags valid YAML as
    ``Missing property "type"``. The shorthand branch keeps the remaining panel properties, so
    the editor still completes and validates ``title`` & co. inside it.

    *surface* and *builtins* put the surface's **vocabulary** in the schema, as an ``enum`` of
    valid ``type`` values. Without it the vocabulary exists only inside a pydantic field validator,
    which no JSON Schema consumer can see -- so the editor could not complete a panel type, and an
    agent reading ``get_config_schema`` was told nothing at all. It is the installed set, from the
    same entry points :func:`check_panel_type` validates against, so the schema and the validator
    cannot disagree.
    """
    default = handler(core_schema)
    props = {k: v for k, v in default.get('properties', {}).items() if k != 'type'}
    known = sorted(builtins | panel_type_names(surface) | {CUSTOM_PANEL_TYPE})
    if known:
        default = {**default,
                   'properties': {**default.get('properties', {}),
                                  'type': {**default.get('properties', {}).get('type', {}),
                                           'enum': known}}}
    return {
        'anyOf': [
            {'type': 'string'},
            default,
            {
                'type': 'object',
                'minProperties': 1,
                'maxProperties': 1,
                'propertyNames': {'enum': known} if known else {},
                'additionalProperties': {
                    'anyOf': [
                        {'type': 'null'},
                        {'type': 'object', 'properties': props, 'additionalProperties': True},
                    ],
                },
            },
        ],
    }


def check_panel_type(value: str, builtins: frozenset, surface: str, allow_custom: bool) -> str:
    """Validate a panel ``type`` against *surface*'s vocabulary, or raise.

    Valid types are the surface's core built-ins plus the installed package panels registered
    for it, plus ``custom`` where that surface supports a user-authored bundle. Package types
    come from the entry points so validation matches what the runtime can actually serve --
    ``costmap`` is valid only when ``robovast_nav`` is installed.

    The surface is part of the check because the two panel kinds take different props: naming a
    run panel in ``visualization.config.panels`` would mount a component that reads a playback
    clock against a resolved configuration, which is a blank panel at best.
    """
    allowed = builtins | panel_type_names(surface)
    if allow_custom:
        allowed = allowed | {CUSTOM_PANEL_TYPE}
    if value in allowed:
        return value

    where = _PANEL_SURFACE_LABELS[surface] if surface in _PANEL_SURFACE_LABELS else surface
    # A panel that exists on the *other* surface is the likeliest mistake here, and "unknown panel
    # type ... (requires the providing plugin installed)" is the worst possible answer to it: the
    # plugin IS installed, and it ships exactly the panel that was named. Say where it belongs
    # instead. Membership is tested rather than assumed disjoint -- ``scene3d`` is valid on both.
    for other, label in _PANEL_SURFACE_LABELS.items():
        if other == surface:
            continue
        if value in (BUILTIN_PANEL_TYPES if other == DEFAULT_PANEL_SURFACE
                     else BUILTIN_CONFIG_PANEL_TYPES) | panel_type_names(other):
            raise ValueError(
                f"{value!r} is a {label} panel, not a {where} one; declare it under "
                f"{_PANEL_SURFACE_BLOCKS[other]}. The {where} panels are: "
                f"{', '.join(sorted(allowed))}")
    raise ValueError(
        f"unknown {where} panel type {value!r}; expected one of {', '.join(sorted(allowed))} "
        f"(package panels require the providing plugin, e.g. 'robovast_nav', installed)")


#: The panel type for a user-authored panel shipped as a built bundle next to the
#: ``.vast`` (referenced by its ``remote``/``module`` fields rather than by a registered
#: type name).
CUSTOM_PANEL_TYPE = "custom"

#: The panel type that renders an author-supplied Vega-Lite spec over a ``data.db`` table. Its
#: ``vega_lite``/``source`` bindings are validated by ``RunViewPanelConfig._vega_needs_bindings``.
VEGA_PANEL_TYPE = "vega"

#: The row ceiling every JSON data query is clamped to
#: (:func:`robovast.results_processing.data_query.query_data_db`). A panel asking for more gets this,
#: silently — hence the check below rather than a number that looks honoured.
DATA_QUERY_ROW_CAP = 5000


def panel_source_problems(props):
    """``(field suffix, message)`` for a panel's row-cap and thinning bindings.

    Shared by :meth:`RunViewPanelConfig._vega_needs_bindings` and ``config_validation``: the two report
    differently (raise on the first vs. collect every one), but there is no reason for them to
    disagree about what is wrong.

    Both of these fail *quietly* at replay time — a bad ``decimate_hz`` reaches the SQL as a bucket
    width and an over-large ``max_rows`` is clamped — so the panel draws a plausible chart of the
    wrong rows. Better to say so while the author is still holding the ``.vast``.
    """
    problems = []
    source = props.get("source")
    if isinstance(source, dict) and source.get("decimate_hz") is not None:
        hz = source["decimate_hz"]
        try:
            value = float(hz)
        except (TypeError, ValueError):
            value = float("nan")
        if isinstance(hz, bool) or not math.isfinite(value) or value <= 0:
            problems.append((
                "source.decimate_hz",
                "'decimate_hz' keeps one sample per 1/hz second, so it must be a number > 0, "
                f"got {hz!r}; 0 would collapse the whole run into a single sample"))
    max_rows = props.get("max_rows")
    if isinstance(max_rows, int) and not isinstance(max_rows, bool) \
            and max_rows > DATA_QUERY_ROW_CAP:
        problems.append((
            "max_rows",
            f"'max_rows' is clamped to {DATA_QUERY_ROW_CAP} by the data query, so {max_rows} does "
            "not buy more of the run: the cap cuts at the head (ORDER BY time LIMIT n) and the "
            "chart ends mid-run. Set 'source.decimate_hz' to thin the whole run instead"))
    return problems


class PanelPosition(BaseModel):
    """Where a panel sits in the run-view. ``anchor`` docks it against an edge, floats it
    at a corner or centered along an edge, or centres it; ``fill`` is used *instead of* an
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


class PanelConfigBase(BaseModel):
    """What every panel declaration shares, on whichever surface it is declared.

    A panel is named by ``type`` and configured by *bindings* -- extra keys this model keeps
    (``extra='allow'``) for the panel plugin to interpret. Only the layout grammar differs between
    surfaces, so it is the subclasses that add fields: the config column has a ``height``, the run
    view has anchors and drag/resize.

    Subclasses set :attr:`SURFACE` and :attr:`BUILTINS`; everything keyed on those -- the type
    check and its diagnosis, the shorthand, the JSON Schema the editor completes from -- is written
    once here. A further surface is then a subclass and two class attributes, which is the point:
    the three things that consult the surface must not be written per surface and drift.
    """

    model_config = ConfigDict(extra='allow')

    #: Which surface this declaration belongs to (``"run"`` / ``"config"``), matching a panel
    #: plugin class's own ``SURFACE``.
    SURFACE: ClassVar[str] = DEFAULT_PANEL_SURFACE
    #: The core panel types valid on this surface; package types come from the entry points.
    BUILTINS: ClassVar[frozenset] = frozenset()

    type: str
    title: Optional[str] = None
    hidden: Optional[bool] = None
    #: For ``type: custom`` -- path (relative to the ``.vast``) to the built panel bundle.
    remote: Optional[str] = None
    #: For ``type: custom`` -- the exposed Module-Federation module to render.
    module: Optional[str] = None

    @model_validator(mode='before')
    @classmethod
    def _flatten_shorthand(cls, v):
        return flatten_panel_shorthand(v)

    @classmethod
    # pydantic's documented hook signature
    def __get_pydantic_json_schema__(cls, core_schema, handler):  # pylint: disable=arguments-differ
        return panel_json_schema(core_schema, handler, surface=cls.SURFACE,
                                 builtins=cls.BUILTINS)

    @field_validator('type')
    @classmethod
    def _known_type(cls, v):
        return check_panel_type(v, cls.BUILTINS, cls.SURFACE, allow_custom=True)

    @model_validator(mode='after')
    def _declared_bindings_are_known(self):
        """Check the panel's own keys against the model its type declares, where it declares one.

        Opt-in per type on purpose. The alternative -- ``extra='forbid'`` for everyone -- would
        refuse every run-view panel's bindings until each was modelled, and the point here is the
        failure mode being fixed: an unknown key used to validate cleanly and draw nothing, so the
        only symptom was an empty panel in the browser with nothing naming the key that was ignored.
        """
        model = bindings_model_for(self.type, self.SURFACE)
        if model is None:
            return self
        extra = self.__pydantic_extra__ or {}
        try:
            model.model_validate(extra)
        except ValidationError as err:
            fields = ", ".join(sorted(model.model_fields)) or "none"
            raise ValueError(
                f"invalid binding for panel {self.type!r}; its fields are: {fields}. {err}"
            ) from None
        return self

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


class RunViewPanelConfig(PanelConfigBase):
    """One panel of the web **run-view**: the free-floating, clock-driven surface.

    ``type`` is one of the core built-ins (:data:`BUILTIN_PANEL_TYPES`), a package-provided
    panel registered in the :data:`PANEL_TYPES_GROUP` entry-point group, or
    :data:`CUSTOM_PANEL_TYPE` for a user-authored panel shipped as a built bundle next to
    the ``.vast``. The panel's own data bindings (e.g. ``layers``/``source`` naming ``data.db``
    tables) are extra keys, interpreted by that plugin.

    Everything a config-view panel also has lives on :class:`PanelConfigBase`; what is here is
    this surface's layout grammar -- anchors, drag and resize -- plus the ``vega`` binding check."""

    SURFACE: ClassVar[str] = DEFAULT_PANEL_SURFACE
    BUILTINS: ClassVar[frozenset] = BUILTIN_PANEL_TYPES

    position: Optional[PanelPosition] = None
    #: Whether the panel's free edge/corner can be dragged to resize it in the run-view.
    #: Defaults to on for every panel type that does not turn it off (the docked playback
    #: bar, the full-view 3D background).
    resizable: Optional[bool] = None
    minimizable: Optional[bool] = None
    minimized: Optional[bool] = None
    #: Lock the panel's geometry: the run-view lets a panel be dragged by its title bar and
    #: resized by its free edge, and ``fixed: true`` opts this one out of both.
    fixed: Optional[bool] = None

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
        for _field, message in panel_source_problems(extra):
            raise ValueError(message)
        return self


class TimelineConfig(BaseModel):
    """Which ``data.db`` table + column defines the run's playback timeline. Set this
    for non-ROS runs whose time lives in a table other than the nav defaults (e.g. a
    sim's ``trajectory`` table with a ``t`` column); when omitted the UI derives the
    range from the standard ``poses``/``behaviors``/``scenario_timestamps`` tables."""
    model_config = ConfigDict(extra='forbid')
    table: str
    time_column: str = 'timestamp'


def _last_member_may_omit_size(panels, describe, size_name: str, of_what: str):
    """Refuse a stack where a member that takes "the rest" is not the last one.

    A vertical stack sizes its members in order and one without a size takes whatever is left,
    so anything declared after it would be laid out on top of it. Shared by the run view's
    ``left``/``right`` gutters and the config view's single column, which are the same rule
    about the same layout arithmetic.
    """
    for i, panel in enumerate(panels[:-1]):
        if describe(panel) is None:
            raise ValueError(
                f"panel '{panel.type}' is {of_what} with no {size_name}, so it takes the rest "
                f"of the column and the {len(panels) - i - 1} panel(s) after it would land on "
                f"top of it. Give it a {size_name}, or make it the last one."
            )


class ConfigPanelConfig(PanelConfigBase):
    """One panel of the web **config view** -- the Config tab's third column, which shows what
    a selected generated configuration contains.

    Same shorthand and the same "extra keys are this panel's data bindings" rule as
    :class:`RunViewPanelConfig`, but a much smaller layout grammar: the config view is one column,
    so a panel declares only its ``height``. There are no anchors and no drag/resize -- the column
    is the campaign author's declared order.
    """

    SURFACE: ClassVar[str] = CONFIG_PANEL_SURFACE
    BUILTINS: ClassVar[frozenset] = BUILTIN_CONFIG_PANEL_TYPES

    #: Pixels (int) or a percentage of the column (``"35%"``). Omit on the last panel to give it
    #: whatever the ones above it left over.
    height: Optional[int | str] = None


class ConfigViewConfig(BaseModel):
    """The Config tab's third column: what each generated configuration contains."""
    model_config = ConfigDict(extra='forbid')
    panels: Optional[list[ConfigPanelConfig]] = Field(default_factory=list)

    @field_validator('panels', mode='before')
    @classmethod
    def _default_empty(cls, v):
        return [] if v is None else v

    @model_validator(mode='after')
    def _column_members_sized(self):
        visible = [p for p in (self.panels or []) if not p.hidden]
        _last_member_may_omit_size(visible, lambda p: p.height, 'height', 'in the config column')
        return self


class RunViewConfig(BaseModel):
    """The web run-view: an ordered list of panels for replaying a single run of a
    postprocessed campaign over its timeline. Rendered by the UI from the campaign's
    snapshot ``.vast``; each panel reads existing ``data.db`` tables."""
    model_config = ConfigDict(extra='forbid')
    timeline: Optional[TimelineConfig] = None
    panels: Optional[list[RunViewPanelConfig]] = Field(default_factory=list)

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
            _last_member_may_omit_size(
                members, lambda p: p.position.height, 'height', f"in the '{side}' column")
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


class ExplorerConfig(BaseModel):
    """The Results **Explorer**: analysis notebooks, executed server-side per selected tree
    node (campaign / batch / config / run) and rendered as HTML."""
    model_config = ConfigDict(extra='forbid')
    notebooks: Optional[list[dict[str, Any]]] = None


class DataBrowserConfig(BaseModel):
    """The Results **Data browser**: campaign-scoped declared plots (see :class:`PlotSpec`)."""
    model_config = ConfigDict(extra='forbid')
    plots: Optional[list[PlotSpec]] = None


class ResultsViewConfig(BaseModel):
    """What the Results tab draws, one key per sub-view."""
    model_config = ConfigDict(extra='forbid')
    run_view: Optional[RunViewConfig] = None
    explorer: Optional[ExplorerConfig] = None
    data_browser: Optional[DataBrowserConfig] = None


class VisualizationConfig(BaseModel):
    """Everything the web UI draws for this campaign, shaped like the UI itself.

    One key per place a declaration lands -- the Config tab, and the Results tab's three
    sub-views -- so reading a ``.vast`` says *where* each block appears. This replaced a flat
    ``visualization.panels`` beside an unrelated top-level ``evaluation:`` block, which said
    neither.
    """
    model_config = ConfigDict(extra='forbid')
    config: Optional[ConfigViewConfig] = None
    results: Optional[ResultsViewConfig] = None


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
    #: Everything the web UI draws, shaped like the UI (see :class:`VisualizationConfig`):
    #: ``config.panels``, ``results.run_view``, ``results.explorer``, ``results.data_browser``.
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


#: What a version 1 config has to become, as human-readable instructions.
#:
#: This was once the *only* migration path -- there was no tool and no v1 reader. Both now
#: exist (``robovast.common.migrations``), so the text is no longer load-bearing for
#: recovery; it stays because it explains the restructuring in one screen, which a caller
#: staring at a refusal still wants. ``migrations/config/v1_to_v2.py`` is the executable
#: form and must agree with it. Keep each entry as "what you wrote" -> "what it becomes".
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
    from robovast.common.migrations import (  # pylint: disable=import-outside-toplevel
        BASELINE_CONFIG_VERSION, SUPPORTED_CONFIG_VERSION, find_migration_markers)

    logger.debug("Validating configuration")
    version = config.get("version", None)
    # This function is the STRICT read policy: authoring and launching a new campaign must
    # not silently accept an old version. Reading an *archived* campaign goes through
    # ``load_config(upgrade=True)`` instead, which ladders it in memory. The refusal below
    # therefore names that path rather than being a dead end -- see migrations/README.md.
    if isinstance(version, int) and BASELINE_CONFIG_VERSION <= version < SUPPORTED_CONFIG_VERSION:
        raise ValueError(
            f"config version {version} is not the current version "
            f"({SUPPORTED_CONFIG_VERSION}), and authoring requires the current one.\n"
            "\n"
            "  Upgrade the file:   vast configuration upgrade\n"
            "\n"
            "An archived campaign is migrated automatically when read, so this refusal "
            "only ever applies to a file you are authoring or launching from.\n"
            "\n" + _V1_MIGRATION)
    if version != SUPPORTED_CONFIG_VERSION:
        # Raised, not logged-and-raised: every caller reports the failure it catches,
        # so logging the same text here printed it twice.
        raise ValueError(f"Unsupported config version: {version}")
    # A work order is not a config. `vast configuration upgrade` and the retrigger's
    # --to-workspace path leave a marker wherever a migration could not carry something forward,
    # and a file still holding one describes a *different experiment* than the campaign it came
    # from. Refusing here means it can never be launched from by any path, which is stronger than
    # relying on the author having run the validator.
    markers = find_migration_markers(config)
    if markers:
        raise ValueError(
            f"{len(markers)} unresolved migration marker(s): this config was migrated as far as "
            f"it could be and still needs a decision at each of these:\n"
            + "\n".join(f"  {where}: {reason}" for where, reason in markers)
            + "\nResolve them and remove the markers; 'vast configuration validate' lists what "
              "is left.")
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
