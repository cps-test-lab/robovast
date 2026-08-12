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

import copy
import logging
from dataclasses import dataclass, field
from typing import Any, ClassVar, Dict, List, Optional

from pydantic import model_validator

from ..common import get_scenario_parameters
from ..config import VariationConfig, get_validated_config

logger = logging.getLogger(__name__)

#: The two channels a variation may write to, and the key that names each.
SCENARIO_CHANNEL = "scenario"
SIM_CHANNEL = "sim"


class VariationInfeasibleError(RuntimeError):
    """A specific parameter draw cannot be realized (e.g. no valid obstacle
    placement exists), as opposed to a bug in the plugin itself. The only
    exception type composition is allowed to treat as "skip this one config"
    rather than a fatal error.

    ``config_name`` is filled in by composition (a plugin does not know which
    config block it is running for) so a reporting layer can name it in its own
    structured field rather than only inside the message.

    ``include_traceback = False`` (see :func:`robovast.common.status.failure_detail`):
    the message names the plugin, the config and the reason, which is the whole of
    what a reader can act on. A stack trace through the composition internals only
    makes an unrealizable parameter combination look like a RoboVAST crash.
    """

    include_traceback = False

    def __init__(self, message, config_name=None):
        super().__init__(message)
        self.config_name = config_name


class DestinationConfig(VariationConfig):
    """Config base for a variation whose outputs the author binds to channels.

    ``scenario:`` and ``sim:`` sit at the same level and the key *is* the destination, so a
    factor's channel is readable from the line it is written on. A ``scenario:`` name is
    checked against the scenario file; a ``sim:`` path against the simulator backend's
    schema. There is no default side and no mode flag: with one, the commonest line in a
    ``.vast`` would have two spellings and a reader would have to look upwards to know
    where a value lands.

    **One output** -- :attr:`SLOTS` empty -- takes a bare name, and exactly one channel:

    .. code-block:: yaml

        - ParameterVariationList:
            sim: config                        # the simulator's world
            values: [world/depot.yaml, world/warehouse.yaml]
        - ParameterVariationList:
            scenario: goal_pose                # a parameter the .osc declares
            values: [...]

    **Several outputs** -- :attr:`SLOTS` naming them -- take a *slot to destination*
    mapping, and may use both channels at once. A plugin producing artifacts that live on
    opposite sides of the compile boundary needs exactly that:

    .. code-block:: yaml

        - FloorplanGeneration:
            floorplans: [environments/secorolab/secorolab.fpm]
            scenario:
              map: map_file                    # nav2 reads it at run time
            sim:
              mesh: plugins.floorplan.mesh     # MuJoCo compiles it in

    Slots exist because a multi-output plugin's destinations cannot be spelled by one key,
    and because their *names* are often dynamic -- a mode flag picking ``goal_pose`` versus
    ``goal_poses``, or a campaign naming the parameter it wants written. Binding them by
    slot puts the name in the ``.vast``, where a reader and a validator can both see it,
    instead of leaving it implied by a positional list whose order has to be remembered.
    """

    #: The output names this variation produces, or ``()`` for a single unnamed output.
    #: Declared by the plugin, so an unknown slot in a ``.vast`` is refused naming the ones
    #: that exist rather than being silently ignored. Every one of these must be bound.
    SLOTS: ClassVar[tuple] = ()

    #: Outputs a campaign may bind but need not. For an output that only *makes sense* in some
    #: deployments -- obstacle geometry for the simulator to compile, which a campaign running a
    #: simulator that spawns at run time does not need -- requiring it would force every campaign
    #: to bind something it has no destination for. Unbound simply means the plugin does not
    #: produce it; an unknown slot is still refused.
    OPTIONAL_SLOTS: ClassVar[tuple] = ()

    scenario: str | list[str] | dict[str, str] | None = None
    sim: str | list[str] | dict[str, str] | None = None

    @model_validator(mode="before")
    @classmethod
    def _refuse_retired_name(cls, data):
        """Reject the retired ``name:`` spelling *before* validation, not as a field.

        Declaring ``name`` in order to refuse it would put it in the rendered schema, so
        every plugin listing and every ``get_plugin_details`` would advertise a key that is
        always an error. Checked on the raw input instead, so the schema shows only the two
        keys that work.
        """
        if isinstance(data, dict) and data.get("name") is not None:
            raise ValueError(
                "'name' no longer names a destination: write 'scenario: <parameter>' for a "
                "parameter the scenario file declares, or 'sim: <key>' for the simulator's "
                "own configuration")
        return data

    @model_validator(mode="after")
    def _destinations_are_bound(self):
        given = [k for k in (SCENARIO_CHANNEL, SIM_CHANNEL) if getattr(self, k) is not None]
        if not self.SLOTS:
            if len(given) != 1:
                raise ValueError(
                    "exactly one of 'scenario' or 'sim' must name this variation's "
                    f"destination, got {given or 'neither'}")
            if isinstance(getattr(self, given[0]), dict):
                raise ValueError(
                    f"'{given[0]}' takes a parameter name, not a mapping: this variation "
                    "produces a single output, so there are no slots to bind")
            return self

        bound: dict = {}
        for channel in given:
            value = getattr(self, channel)
            if not isinstance(value, dict):
                raise ValueError(
                    f"'{channel}' takes a mapping of slot to destination for this "
                    f"variation, whose outputs are: {', '.join(self.SLOTS)}")
            for slot in value:
                if slot not in self.SLOTS and slot not in self.OPTIONAL_SLOTS:
                    raise ValueError(
                        f"'{slot}' is not an output of this variation; its outputs are: "
                        + ", ".join((*self.SLOTS, *self.OPTIONAL_SLOTS)))
                if slot in bound:
                    raise ValueError(
                        f"output '{slot}' is bound to both 'scenario' and 'sim'; an output "
                        "goes to one channel")
                bound[slot] = channel
        missing = [s for s in self.SLOTS if s not in bound]
        if missing:
            raise ValueError(
                "every output must be bound to a channel; unbound: "
                + ", ".join(missing))
        return self

    @property
    def channel(self) -> str:
        """Which channel a single-output variation writes to."""
        return SCENARIO_CHANNEL if self.scenario is not None else SIM_CHANNEL

    @property
    def destination(self) -> str | list[str]:
        """The parameter name(s) or dotted ``sim`` path(s) a single-output variation writes."""
        return self.scenario if self.scenario is not None else self.sim

    def is_bound(self, slot: str) -> bool:
        """Whether the campaign bound *slot* to a channel.

        Only meaningful for :attr:`OPTIONAL_SLOTS`: a required slot is always bound, because
        validation refuses the config otherwise.
        """
        return any(isinstance(getattr(self, c), dict) and slot in getattr(self, c)
                   for c in (SCENARIO_CHANNEL, SIM_CHANNEL))

    def binding(self, slot: str) -> tuple:
        """``(channel, destination)`` for one output slot."""
        for channel in (SCENARIO_CHANNEL, SIM_CHANNEL):
            mapping = getattr(self, channel)
            if isinstance(mapping, dict) and slot in mapping:
                return channel, mapping[slot]
        raise KeyError(
            f"'{slot}' is not an output of this variation; its outputs are: "
            + ", ".join((*self.SLOTS, *self.OPTIONAL_SLOTS)))

    def outputs(self) -> dict:
        """``{channel: [destination, ...]}`` -- what this variation writes and where.

        The answer :meth:`Variation.declared_outputs` gives for every config built on this
        class, so validation, ``preview_configurations`` and the rendered plugin docs all
        read one description instead of each inferring one.
        """
        result: dict = {SCENARIO_CHANNEL: [], SIM_CHANNEL: []}
        for channel in (SCENARIO_CHANNEL, SIM_CHANNEL):
            value = getattr(self, channel)
            if value is None:
                continue
            if isinstance(value, dict):
                result[channel].extend(str(v) for v in value.values())
            elif isinstance(value, list):
                result[channel].extend(str(v) for v in value)
            else:
                result[channel].append(str(value))
        return {k: v for k, v in result.items() if v}

# Module-level counter for generating short, unique config indexes.
# All variation classes can call `get_config_index()` to obtain a new
# sequential index. Call `reset_config_index()` to reset back to 0
# (the next `get_config_index()` will return 1).
_config_index = 0  # pylint: disable=invalid-name


def reset_config_index():
    """Reset the shared config index back to zero.

    This should be called whenever a new Variation instance (or new
    variation run) starts so generated short names begin at
    `config1` again.
    """
    global _config_index  # pylint: disable=global-statement
    _config_index = 0


def get_config_index():
    """Return the next unique config index (1-based).

    Thread-safe.
    """
    global _config_index  # pylint: disable=global-statement
    _config_index += 1
    return _config_index


def _to_cache_jsonable(value):
    """Convert value to JSON-serializable form for cache key hashing."""
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    if isinstance(value, dict):
        return {k: _to_cache_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_cache_jsonable(v) for v in value]
    if isinstance(value, bytes):
        return value.hex()
    if hasattr(value, "model_dump") and callable(getattr(value, "model_dump")):
        return _to_cache_jsonable(value.model_dump())
    return str(value)


@dataclass
class ProvContribution:
    """Domain-specific PROV-O graph contributions from a variation plugin.

    Returned by :meth:`Variation.collect_prov_metadata` to inject
    domain-specific provenance nodes into the campaign's PROV-O graph.

    Attributes:
        graph_nodes: Extra PROV graph node dicts (entities, activities)
            to append to the ``@graph`` list.
        scenario_properties: Properties to merge onto the concrete
            scenario entity node for this configuration.
        run_used_iris: IRIs of entities that each run activity should
            declare as ``used``.
    """

    graph_nodes: List[Dict[str, Any]] = field(default_factory=list)
    scenario_properties: Dict[str, Any] = field(default_factory=dict)
    run_used_iris: List[str] = field(default_factory=list)


class Variation():

    CONFIG_CLASS = None  # Pydantic model class for config validation
    GUI_CLASS = None  # Could be set to a GUI class for editing (desktop, PySide6)
    GUI_RENDERER_CLASS = None  # Could be set to a GUI renderer class (desktop, PySide6)
    #: Web config editor: package-relative dir of a built Module-Federation remote
    #: (``remoteEntry.js`` + chunks) exposing a ``./preview`` React component
    #: ``({config}) => JSX``. ``None`` = no web preview (the editor renders the
    #: resolved config, or a host-native preview for built-in types).
    WEB_PREVIEW = None
    CACHE_ID = None  # Subclasses set to enable caching (e.g. "robovast_mt_generation_")

    def __init__(self, base_path, parameters, general_parameters, progress_update_callback,
                 scenario_file, output_dir, container_runner=None):
        # Reset shared config index for each new Variation instance so
        # generated short names start from 1 for this variation run.
        reset_config_index()
        self.base_path = base_path
        if self.CONFIG_CLASS is not None:
            self.parameters = get_validated_config(parameters, self.CONFIG_CLASS)
        else:
            self.parameters = parameters
        self.general_parameters = general_parameters
        self.progress_update_callback = progress_update_callback
        self.scenario_file = scenario_file
        self.output_dir = output_dir
        # Auxiliary-container handle, provided by the execution backend when this
        # plugin declared one via get_required_container(); None otherwise.
        self.container_runner = container_runner
        # Track the next index for each parent config name
        self._config_child_indices = {}

    def variation(self, in_configs):
        # vary in_configs and return result
        return None

    def update_destination(self, config, values: dict, **kwargs):
        """:meth:`update_config`, routing *values* to the channel this variation names.

        For the generic plugins, whose :attr:`CONFIG_CLASS` is a :class:`DestinationConfig`:
        the one place that maps ``scenario:`` / ``sim:`` onto ``scenario_values`` /
        ``sim_values``, so four plugins do not each spell the branch out.
        """
        if getattr(self.parameters, "channel", SCENARIO_CHANNEL) == SIM_CHANNEL:
            return self.update_config(config, {}, sim_values=values, **kwargs)
        return self.update_config(config, values, **kwargs)

    def update_slots(self, config, values_by_slot: dict, **kwargs):
        """:meth:`update_config`, routing each output slot to the channel it is bound to.

        The multi-output counterpart of :meth:`update_destination`. A plugin says *what* it
        produced, by slot; where each one lands is the author's binding, resolved here. That
        is what lets one plugin write to both channels in a single call -- which a plugin
        whose artifacts straddle the compile boundary has to do, and has to do atomically:
        the world compiling six obstacles and the trial driving six of them are one fact.
        """
        scenario_values: dict = {}
        sim_values: dict = {}
        for slot, value in values_by_slot.items():
            channel, destination = self.parameters.binding(slot)
            target = sim_values if channel == SIM_CHANNEL else scenario_values
            target[destination] = value
        return self.update_config(config, scenario_values, sim_values=sim_values, **kwargs)

    @classmethod
    def declared_outputs(cls, parameters) -> dict:
        """``{channel: [destination, ...]}`` this variation will write, for *parameters*.

        Takes the plugin's own config because the names are frequently **dynamic**: a mode
        flag picks ``goal_pose`` versus ``goal_poses``, and several plugins let the campaign
        name the parameter they write. A static class attribute could not answer for those,
        and answering wrongly is worse than not answering.

        Three things read this, and none of them can work it out for itself: validation
        checks scenario outputs against the scenario file and ``sim`` outputs against the
        backend, ``preview_configurations`` shows which factor produced which value on which
        channel, and the rendered plugin docs list the destinations.

        ``{}`` -- the default -- means *undeclared*, not *nothing*. A third-party plugin that
        does not implement it keeps working; its outputs are simply not pre-checked, which is
        exactly today's behaviour for every plugin.
        """
        outputs = getattr(parameters, "outputs", None)
        return outputs() if callable(outputs) else {}

    @classmethod
    def get_required_container(cls, parameters):
        """Declare an auxiliary container this variation needs while it runs.

        Override in subclasses that must run a helper image (e.g. a mesh/map
        generator) during :meth:`variation`. Return a
        :class:`~robovast.common.variation.container_runner.ContainerSpec`, or
        ``None`` (the default) to require no container.

        The declaration is backend-agnostic: locally the backend satisfies it
        with an ephemeral ``docker run``; in-cluster it adds a container to the
        campaign's auxiliary pod. Either way the plugin talks to it through
        ``self.container_runner`` (see
        :mod:`robovast.common.variation.container_runner`).

        Args:
            parameters: The raw (unvalidated) parameter dict for this variation
                block, so the image can depend on config (e.g. a pinned version).

        Returns:
            A ``ContainerSpec`` or ``None``.
        """
        return None

    def get_cache_input_files(self, in_configs):
        """Return file paths that affect variation output. Override when using CACHE_ID."""
        return []

    def get_input_files(self):
        """Return relative file paths (relative to base_path) required as input.

        Override in subclasses to report files that this variation consumes from
        the source directory. These files will be copied into the campaign
        ``_config/`` directory to make the campaign self-contained.

        Returns:
            list[str]: Relative file paths (relative to ``self.base_path``).
        """
        return []

    def get_campaign_transient_files(self):
        """Return intermediate files to be placed in the campaign-level ``_transient/`` directory.

        Override in subclasses to report files created as intermediate artifacts
        during the variation step that are campaign-wide (not specific to a single
        config).  These files will be copied into ``campaign-<id>/_transient/``.

        Must be called after :meth:`variation` has been executed.

        Returns:
            list[tuple[str, str]]: List of ``(relative_path, absolute_path)`` tuples.
                ``relative_path`` is the destination inside ``_transient/``.
        """
        return []

    def progress_update(self, msg):
        self.progress_update_callback(f"{self.__class__.__name__}: {msg}")

    def update_config(self, config, scenario_values, config_files: list = None,
                      other_values=None, sim_values=None):
        """Produce the next configuration from *config*, with this variation's values on it.

        A variation writes to **two channels**, and which one a value belongs to is decided
        by when the simulator can still act on it:

        ``scenario_values``
            what the *trial* does -- goals, poses, protocol. Delivered as scenario
            parameters and checked against the ``.osc``.
        ``sim_values``
            what the trial *runs in* -- a ``{dotted destination: value}`` mapping against
            the simulator backend's own schema. Delivered as the resolved ``sim`` block and
            checked against that backend.

        They are arguments of **one** call rather than two knobs because a variation
        frequently writes both and they must agree: MuJoCo does not recompile mid-run, so a
        trial that drives six obstacles needs a world that compiled six. Splitting them into
        separate calls would let a plugin update one and forget the other.

        *other_values* remains what it was -- top-level metadata keys such as ``_map_file``
        that readers of the configuration use and the run never sees.
        """
        new_config = copy.deepcopy(config)

        # Ensure config dict exists
        if 'config' not in new_config:
            new_config['config'] = {}

        # Add parameters to config
        for key, val in scenario_values.items():
            new_config['config'][key] = val

        # The simulation channel. Flat and dotted, never nested: an authored ``sim:`` block
        # is flattened to the same shape before it gets here, so a configuration carries one
        # kind of thing and a reader is never guessing which. Resolution against the
        # backend's DOTTED_ROOT happens once, later, where the backend is known.
        if sim_values:
            new_config.setdefault('sim', {}).update(sim_values)

        # Add other parameters to config
        if other_values:
            for key, val in other_values.items():
                new_config[key] = val

        # Ensure config_files list exists
        if '_config_files' not in new_config:
            new_config['_config_files'] = []

        new_config['_config_files'].extend(config_files or [])

        # Update config name with automatic per-parent indexing
        parent_name = config['name']
        # Automatically track index per parent config
        if parent_name not in self._config_child_indices:
            self._config_child_indices[parent_name] = 1
        local_index = self._config_child_indices[parent_name]
        self._config_child_indices[parent_name] += 1

        new_config['name'] = f"{parent_name}-{local_index}"
        return new_config

    @classmethod
    def collect_config_metadata(cls, config_entry: dict, config_dir, campaign_dir) -> dict:
        """Return additional metadata fields for a configuration entry.

        Called during metadata generation for each configuration that used
        this variation.  Override in subclasses to attach domain-specific
        metadata (e.g. map or mesh information).

        Args:
            config_entry: The configuration dict from ``configurations.yaml``.
            config_dir: :class:`~pathlib.Path` to
                ``<campaign>/<config-name>/``.
            campaign_dir: :class:`~pathlib.Path` to ``campaign-<id>/``.

        Returns:
            Dictionary of fields to merge into the configuration's metadata
            entry, or an empty dict.
        """
        return {}

    @classmethod
    def collect_prov_metadata(
        cls,
        config_entry: dict,
        campaign_namespace,
        config_namespace,
        gen_activity_id: str,
        vast_id: str,
    ) -> Optional["ProvContribution"]:
        """Return domain-specific PROV-O graph contributions.

        Called during PROV-O generation for each configuration that used
        this variation.  Override in subclasses to contribute
        domain-specific provenance nodes (e.g. map entities, generation
        activities).

        Args:
            config_entry: The configuration metadata dict.
            campaign_namespace: :class:`rdflib.Namespace` for the campaign.
            config_namespace: :class:`rdflib.Namespace` for this config.
            gen_activity_id: IRI of the config-generation activity.

        Returns:
            A :class:`ProvContribution`, or ``None`` to contribute nothing.
        """
        return None

    def check_scenario_parameter_reference(self, reference_name):
        """Check if a scenario parameter reference exists."""
        parameters = get_scenario_parameters(self.scenario_file)
        if not isinstance(parameters, dict) or not len(parameters) == 1:
            raise ValueError("Unexpected scenario parameters format.")

        parameters = next(iter(parameters.values()))
        for param in parameters:
            if param.get('name') == reference_name:
                return
        raise ValueError(f"Scenario parameter reference '{reference_name}' not found in scenario parameters.")

    def scenario_parameter_is_list(self, name: str) -> bool:
        """Whether the scenario declares *name* as a list -- the shape a value must have.

        The scenario file is the only place that fact is actually defined
        (``goal_poses: list of pose_3d`` versus ``goal_pose: pose_3d``), so a plugin
        producing one-or-many asks here rather than inferring it. Inferring it is what the
        two path variations used to do -- one compared the destination *name* to the literal
        string ``"goal_pose"``, the other keyed on ``num_goal_poses == 1`` -- and both could
        disagree with the scenario, which surfaced as a type error at run time rather than
        as a wrong ``.vast``.

        An undeclared parameter raises, naming it: the alternative is guessing a shape for a
        destination that does not exist.
        """
        parameters = get_scenario_parameters(self.scenario_file)
        if not isinstance(parameters, dict) or len(parameters) != 1:
            raise ValueError("Unexpected scenario parameters format.")
        for param in next(iter(parameters.values())):
            if param.get('name') == name:
                return bool(param.get('is_list'))
        raise ValueError(
            f"Scenario parameter '{name}' is not declared in {self.scenario_file}, so the "
            "shape of the value written to it cannot be determined.")
