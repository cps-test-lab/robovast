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

from robovast.common.quantity import to_bytes, to_cores

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
        "`{overrides: {components: {ceiling: {enabled: false}}}}`). The sibling of "
        "`parameters` for the other channel: `parameters` is what the trial does, `sim` "
        "is what it runs in. Merged over `execution.containers.simulation`, which stays "
        "the campaign-wide default.")
    sut: Optional[dict[str, Any]] = Field(
        default=None,
        description="Fixed values for how the system under test is configured in this "
        "configuration, as a FLAT mapping of `<source>.<path>` to value (e.g. "
        "`{'nav2.local_costmap.local_costmap.ros__parameters.plugins': [...]}`). Flat and "
        "not nested, unlike `sim`: everything after the source name belongs to that file's "
        "format and may be an XPath, which no nested mapping can express. `{$absent: true}` "
        "removes the node. Merged under any variation writing the same destination.")
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

    **Unknown keys are refused.** Pydantic's default is to ignore them, and for this block
    that is the worst of the options: a resource a deployment does not understand is dropped
    in silence, and the campaign runs with a different allocation than its file asks for.
    Not hypothetical: a key added to a tree whose deployed service does not have it yet caps
    the container BELOW the figure it was running at, and no error and no log mentions it. A
    typo (``cpu_limits``) has the same shape and is more likely.

    The cost is that a ``.vast`` using a field a deployment predates now fails to launch
    instead of quietly running differently -- which is the correct trade for a block whose
    whole purpose is to say how much of the machine a run may have.

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
    model_config = ConfigDict(extra='forbid')

    #: The **reservation**: what the cluster packs by, and so what decides how many trials run
    #: at once. With no :attr:`cpu_limit` beside it this is the ceiling as well -- the two are
    #: stamped equal, which is what every campaign meant before that field existed.
    cpu: Optional[Union[int, float, str, list[dict[str, Union[int, float, str]]]]] = None
    #: The memory reservation, and -- with no :attr:`memory_limit` -- the ceiling too. The
    #: shipped examples deliberately never split it: exceeding a CPU limit costs speed,
    #: exceeding a memory limit is an OOM kill.
    memory: Optional[Union[str, list[dict[str, str]]]] = None
    #: The ceiling, when it should differ from the reservation above. Omitted -- the default,
    #: and what every campaign did before these existed -- the limit equals the request.
    #:
    #: **Splitting the two is a decision about the container's ROLE, not a tuning knob.**
    #:
    #: * The **system under test** keeps ``requests == limits``, sized so it does not
    #:   throttle. The property that protects the result is that its ceiling never *binds*:
    #:   an allocation the container never reaches cannot have shaped what the stack did, and
    #:   equality is the conservative way to reach that while nothing measures what it
    #:   actually got. Worth over-reserving for. The peak it is sized from comes from a pilot
    #:   and clipping is not proportional, so a search proposing harder configurations can
    #:   exceed it -- ``run_validity_view.quota_bound`` says when that happened.
    #: * The **simulator and scenario** are not under test and should split. The simulator's
    #:   peak-to-mean ratio is roughly 18 (measured: 0.34 cores sustained, 5.98 at its
    #:   startup burst), so there is no honest single number: reserving the peak costs more
    #:   than the un-tuned campaign did, and capping at the sustained figure clips a burst
    #:   that changes nothing the robot experiences. Realtime pacing already normalises what
    #:   the simulated world looks like, and ``runs.clock_map_*`` records per run whether it
    #:   kept pace -- so the guard that makes a soft limit safe here is already in the data.
    #:
    #: The reservation is what the cluster packs by, so lowering it is what buys concurrency;
    #: the limit only decides when the kernel starts throttling.
    cpu_limit: Optional[Union[int, float, str,
                              list[dict[str, Union[int, float, str]]]]] = None
    memory_limit: Optional[Union[str, list[dict[str, str]]]] = None
    #: Whole GPUs for this container. Omit it and the container running the simulator gets
    #: one wherever the cluster advertises GPUs, so the common case needs nothing here;
    #: ``0`` opts out on a cluster that has them. A real field rather than an undeclared key
    #: because pydantic's default ``extra='ignore'`` was dropping it from the model, so the
    #: documented option only worked where a lane happened to read the raw mapping.
    gpu: Optional[Union[int, list[dict[str, int]]]] = None

    @field_validator('cpu', 'cpu_limit')
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

    @model_validator(mode="after")
    def validate_limits_are_not_below_requests(self):
        """A ceiling under its own reservation is refused here rather than by the cluster.

        Kubernetes rejects such a pod outright, so the campaign would fail at submission with
        an API message naming a container and a quantity -- true, and several layers from the
        two lines in the ``.vast`` that disagree. Only the scalar forms are compared: a
        per-cluster list is resolved per context much later, and guessing which entry pairs
        with which here would report a conflict that the active cluster may not have.
        """
        pairs = (("cpu", self.cpu, self.cpu_limit, to_cores),
                 ("memory", self.memory, self.memory_limit, to_bytes))
        for name, request, limit, convert in pairs:
            if request is None or limit is None:
                continue
            if isinstance(request, list) or isinstance(limit, list):
                continue
            lo, hi = convert(request), convert(limit)
            if lo is None or hi is None or hi >= lo:
                continue
            raise ValueError(
                f"{name}_limit ({limit}) is below {name} ({request}): a limit is the "
                f"ceiling and cannot be under the reservation. Either raise {name}_limit "
                f"or lower {name}.")
        return self


class PostprocessResourcesConfig(BaseModel):
    """What the postprocessing pod's conversion step may have.

    Sizes the step that deserializes a campaign's rosbags -- the only expensive one -- and,
    from the same figure, how many bags it converts at once::

        results_processing:
          resources:
            cpu: 8
            memory: 16Gi

    Per-cluster lists work as they do in :class:`ResourcesConfig` (``cpu: [{ctx: 4}, …]``).

    **The reservation is the ceiling: there is no ``cpu_limit``/``memory_limit`` here.** The
    reason is not efficiency but comparability. This pod runs on the nodes that run trials,
    so a conversion allowed past its reservation takes cores from a run whose own request was
    honest -- and that run's timing then depends on which campaign happened to be
    postprocessing beside it, which is exactly the hidden variable the CPU governor exists to
    remove. Equality costs density, because admission packs by requests, and buys a
    postprocessing step that cannot perturb a measurement.

    **What it sizes is the pod, not one step.** A campaign's ``results_processing`` block is
    mostly not rosbag conversion -- its own metric plugins, ``metadata_processing``,
    ``publication``, ``health_checks`` -- and all of that runs in the pod's host step, beside
    the index ingest. Those are the steps whose appetite RoboVAST cannot know, so this figure
    raises them too. Sizing only the conversion would leave a campaign's own analysis code
    pinned at a figure it could not change, and the symptom would be an OOM kill of a step
    whose declared allocation said it had room.

    Kubernetes charges a pod the *maximum* over its steps rather than their sum here (staging
    and conversion are initContainers), so one figure serving several steps costs nothing.

    **It raises; it does not lower.** The steps that run our own code keep their floors
    whatever a ``.vast`` says. A campaign knows when its analysis needs more than the
    default; it cannot know that the index ingest still fits in less, and being wrong in that
    direction is an OOM kill of the step that publishes the results rather than a slow step.
    So ``cpu: 1`` still yields a pod reserving the floor -- the conversion container itself is
    held to the smaller figure, and its fan-out follows it, but the pod's reservation does not
    fall below what the fixed steps need.

    Staging is the one step this does not touch at all. Its footprint is set by how it is
    written -- one object at a time -- so its small memory bound is a guard rather than a
    reservation, and a limit that grew with whatever a campaign asked for is exactly the one
    that would absorb a regression in that streaming instead of failing on it.

    """
    model_config = ConfigDict(extra='forbid')

    @model_validator(mode="before")
    @classmethod
    def refuse_split_limits(cls, data):
        """Name the reason when a ``.vast`` tries to split request from limit.

        ``extra='forbid'`` already refuses these keys, but its message ("extra inputs are not
        permitted") reads as though the field were misspelled -- and someone writing
        ``cpu_limit`` here has copied a block that is correct one section up, where splitting
        is deliberate. Runs ``before`` because the forbid check would otherwise raise first.
        """
        if not isinstance(data, dict):
            return data
        split = sorted(k for k in ("cpu_limit", "memory_limit") if k in data)
        if split:
            raise ValueError(
                f"{', '.join(split)} cannot be set for postprocessing: here the reservation "
                "is the ceiling. This pod shares nodes with trials, so a conversion allowed "
                "past its request perturbs a run that reserved honestly. Set cpu/memory to "
                "the figure you want the step held to.")
        return data

    #: Cores for the conversion, as request and as limit. Also the worker count: the step
    #: converts one bag per process, and it is this figure -- not what the node happens to
    #: have -- that decides how many run at once.
    cpu: Optional[Union[int, float, str, list[dict[str, Union[int, float, str]]]]] = None
    #: Memory for the conversion, as request and as limit.
    memory: Optional[Union[str, list[dict[str, str]]]] = None

    @field_validator('cpu')
    @classmethod
    def validate_cpu_quantity(cls, v):
        """Reject a cpu value that is not a CPU quantity -- see
        :meth:`ResourcesConfig.validate_cpu_quantity`, which this mirrors for the same
        reason: a bad quantity here surfaces as a pod that never schedules."""
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

    @field_validator('memory')
    @classmethod
    def validate_memory_quantity(cls, v):
        """Reject a memory value that is not a memory quantity."""
        def check(value):
            if to_bytes(value) is None:
                raise ValueError(
                    f'memory {value!r} is not a memory quantity: use a Kubernetes '
                    'quantity ("4Gi", "512Mi")')

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

#: The scenario parameter carrying **this configuration's own directory** in the container.
#:
#: A campaign's per-configuration files are staged under ``<CONFIG_MOUNT>/<config name>/``
#: while the scenario file itself is campaign-wide, so a path written relative to the
#: scenario reaches the campaign's copy of a file and never the cell's. A scenario that
#: declares this parameter is given the cell's directory and can address its own copies
#: directly; the campaign does not set it, and is refused if it tries.
CONFIG_DIR_PARAM = 'config_dir'


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


#: A ref that pins: a full commit sha, or a tag-shaped ref. Deliberately permissive about the
#: tag half (``v1.2.3``, ``release-1.18``, ``2024-05-01``) and deliberately strict about what it
#: refuses -- a bare word that reads like a branch (``main``, ``devel``, ``humble``). We cannot
#: tell a tag from a branch without the network, but the branch names that actually get written
#: by mistake are exactly the ones with no version-ish character in them, so requiring a digit
#: somewhere catches them while never rejecting a real tag anyone writes.
PINNED_REF = re.compile(r"^(?:[0-9a-f]{40}|[^\s]*[0-9][^\s]*)$")


class RosPackageConfig(BaseModel):
    """One git repository colcon-built into the container's ROS overlay.

    For the ROS packages that have no other way in. A package with a ``source:`` entry and no
    ``release:`` block in ``ros/rosdistro`` has no Debian on any distro and is not on PyPI
    either -- ``px4_msgs`` is one, and vendor driver and message packages routinely are -- so
    ``system_packages`` and ``python_packages`` between them cannot express it, and the only
    workaround was baking it into a shared family image where every unrelated campaign pays for
    it.
    """
    model_config = ConfigDict(extra='forbid')

    #: Clone URL. Anything ``git clone`` can reach **without a credential**: the clone runs in
    #: the image build with no token mounted, unlike a ``python_packages`` git spec, whose
    #: BuildKit secret is attached to the pip layer alone. A private repository would fail the
    #: build at the clone -- loudly, naming the repo -- rather than build something incomplete.
    git: str
    #: The commit or tag to build. **Required, and must pin.** A layer's cache key is its command
    #: text, so a branch name would serve whatever the branch pointed at on the day of the first
    #: build, forever -- and nothing in the image would record which commit that was.
    ref: str
    #: Which of the repository's packages to build. Omitted -- the normal case -- means *all of
    #: them*: colcon discovers what the repo contains, so a repo with one package and a repo with
    #: forty are the same declaration. Name packages only to take part of a monorepo.
    packages: Optional[list[str]] = None

    @field_validator('git', 'ref')
    @classmethod
    def _validate_nonblank(cls, v, info):
        if not isinstance(v, str) or not v.strip():
            raise ValueError(f"'{info.field_name}' must be a non-empty string")
        return v.strip()

    @field_validator('ref')
    @classmethod
    def _validate_pinned(cls, v):
        if not PINNED_REF.match(v):
            raise ValueError(
                f"'{v}' looks like a branch, not a pin. Give a commit sha or a release tag: "
                "a branch is re-read only when something else invalidates the layer cache, so "
                "the image would keep serving whatever the branch pointed at on its first build")
        return v

    @field_validator('packages')
    @classmethod
    def _validate_package_names(cls, v):
        if v is None:
            return v
        if not v:
            raise ValueError(
                "'packages' is empty; omit it to build every package the repository contains")
        for i, name in enumerate(v):
            if not isinstance(name, str) or not name.strip():
                raise ValueError(f"package {i} is blank; expected a ROS package name")
        return v


#: The fields whose presence means a container's sizing was declared rather than measured.
#: ``gpu`` is not among them: a device count is a count, not a rate, so nothing measures it
#: and it never decides the mode.
SIZED_FIELDS = ("cpu", "cpu_limit", "memory", "memory_limit")


def infer_sizing(execution) -> str:
    """The sizing mode *execution* means, whether or not it says so.

    **One rule, reachable from both sides.** The model applies it in `resolve_sizing`; the
    cluster lane needs the same answer from the RAW section, because what reaches a backend is
    the parsed YAML rather than the validated model -- so an inferred mode was invisible there
    and every campaign that declared nothing silently ran as `fixed`, which is the opposite of
    what declaring nothing asks for. Written once so the two cannot answer differently.

    *execution* may be the raw mapping or the model; both are read the same way.
    """
    stated = (execution.get("sizing") if isinstance(execution, dict)
              else getattr(execution, "sizing", None))
    if stated:
        return str(stated)
    containers = (execution.get("containers") if isinstance(execution, dict)
                  else getattr(execution, "containers", None)) or {}
    for container in containers.values():
        resources = (container.get("resources") if isinstance(container, dict)
                     else getattr(container, "resources", None))
        if resources is None:
            continue
        for field in SIZED_FIELDS:
            value = (resources.get(field) if isinstance(resources, dict)
                     else getattr(resources, field, None))
            if value is not None:
                return "fixed"
    return "calibrated"


class CalibrationHeadroom(BaseModel):
    """Margin above what a probe measured, per resource.

    Two figures rather than one because the resources fail differently: overrunning a CPU
    reservation slows a container, overrunning a memory one kills it, so memory is normally
    given the larger margin.
    """
    model_config = ConfigDict(extra='forbid')

    cpu: Optional[float] = None
    memory: Optional[float] = None


class CalibrationConfig(BaseModel):
    """How one container's measurement becomes its allocation, under ``sizing: calibrated``.

    **A family, not a setting** -- which percentile, how much margin, whether the container
    may burst past its reservation -- so it is a named block from the start rather than loose
    fields that accrete on :class:`ContainerConfig`. Adding an option later is one optional
    field here, live in the ``.vast`` and in ``ROBOVAST_CALIBRATION`` at once.

    Every field is optional and resolved most-specific-first: this block, then the service's
    ``ROBOVAST_CALIBRATION`` entry for the container's role, then the role's own rule. A
    campaign that states none of it is the normal case.

    **Inert under ``sizing: fixed``**, where nothing is measured. Declaring one there is
    reported as an advisory rather than ignored -- a file carrying settings that do nothing
    reads as configured and behaves as default.
    """
    model_config = ConfigDict(extra='forbid')

    #: The percentile the figure is taken at, over the probe's per-tick samples. ``100`` is
    #: the sample's max -- the 100th percentile of a finite sample *is* its largest value, so
    #: there is one spelling per value and no alias to keep in step. Omitted, the role
    #: decides: 100 for the system under test, 95 for everything else.
    #:
    #: **The probe's throttle tolerance is derived from this**, because clipping cannot move
    #: a figure while it stays inside the tail the percentile already discards. A container
    #: read at 99 therefore tolerates a tenth of the throttling one read at 95 does.
    size_on: Optional[float] = None

    #: Whether the container may burst past its reservation. ``request`` sets the limit equal
    #: to the request, so it never throttles and its budget is the same in every run;
    #: ``declared`` keeps the ceiling from ``resources`` and lets it burst. Omitted, the role
    #: decides: ``request`` for the system under test, ``declared`` for everything else.
    limit: Optional[Literal["request", "declared"]] = None

    #: Margin above the measurement, per resource. Omitted, the built-in constants apply.
    headroom: Optional[CalibrationHeadroom] = None

    @field_validator('size_on')
    @classmethod
    def validate_size_on(cls, v):
        """A percentile, so ``(0, 100]``. Zero would ask for a figure below every sample."""
        if v is None:
            return v
        if not 0 < v <= 100:
            raise ValueError(
                f"execution.containers.<name>.calibration.size_on is a percentile and must "
                f"be greater than 0 and at most 100 (100 is the sample's max); got {v}")
        return v

    @field_validator('headroom')
    @classmethod
    def validate_headroom(cls, v):
        """Below 1.0 it is not headroom -- it would size a container under what it used."""
        for field in ("cpu", "memory"):
            value = None if v is None else getattr(v, field, None)
            if value is not None and value < 1.0:
                raise ValueError(
                    f"execution.containers.<name>.calibration.headroom.{field} is a "
                    f"multiplier on the measured figure and must be at least 1.0; "
                    f"got {value}")
        return v


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
    #: ROS packages built from source into the container's ``/ws`` colcon overlay -- the third
    #: way in, for the packages the other two cannot express (see :class:`RosPackageConfig`).
    #:
    #: Every entry is cloned into the SAME workspace and built in ONE ``colcon build``, which is
    #: the entire reason a workspace exists here: an inter-package or inter-repo dependency then
    #: resolves against the sibling being built beside it, rather than against a released Debian
    #: that for a source-only package does not exist.
    ros_packages: Optional[list[RosPackageConfig]] = None
    #: What the container runs. Omitted for the roles RoboVAST drives itself (the
    #: scenario runner, a sidecar's scenario-execution server); required for an ad-hoc
    #: container, which nothing else knows how to start.
    command: Optional[list[str]] = None
    #: What this container may have: its request under ``sizing: fixed``, and under
    #: ``calibrated`` both what it starts at and the ceiling a measured figure may not
    #: exceed. One meaning in either mode -- the most this container gets.
    resources: Optional[ResourcesConfig] = None
    #: How a measurement becomes this container's allocation. Only read under
    #: ``sizing: calibrated``; see :class:`CalibrationConfig`.
    calibration: Optional[CalibrationConfig] = None
    #: Simulator backend entry point (``simulation`` role only) -- a name in the
    #: ``robovast.simulators`` group, or a ``.vast``-relative ``<file>.py:<Class>`` ref.
    #: The backend's own keys ride alongside it and are validated by its CONFIG_CLASS.
    backend: Optional[str] = None
    #: Configuration files this container reads, ``{source name: path}`` -- what the
    #: ``sut:`` channel addresses. A value is the ``.vast``-relative path, or
    #: ``{file: <path>, format: <name>}`` where the extension does not name the format.
    #:
    #: Named ``config_files`` and not ``config`` because the ``simulation`` container's
    #: ``config`` is a *backend* key (the world it loads, validated by the backend's own
    #: CONFIG_CLASS). One key meaning a backend's world on one container and RoboVAST's
    #: source map on another is a collision rather than a parallel.
    config_files: Optional[dict[str, Union[str, dict[str, str]]]] = None

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
        return bool(self.system_packages or self.python_packages or self.ros_packages)


#: Size of the shared ``/dev/shm`` a run gets when its ``.vast`` does not say.
#:
#: Not a guess: it is eight times the 64 MiB the local lane hands out for free, which is
#: what every campaign has in fact been running inside, and it stays under the threshold at
#: which ``get_campaign_summary`` would advise lowering it -- a default that immediately
#: advised against itself would train the reader to ignore the advice. The pool is a tmpfs
#: charged to the pod, so this is paid on every job of every sweep; a campaign that measures
#: a higher peak declares its own.
DEFAULT_SHM_SIZE = "512Mi"


#: The environment variables RoboVAST sets itself, which a campaign may not write.
#:
#: **What belongs here is the run's own protocol** -- what identifies it, where it writes,
#: what it runs, and the credentials it uploads with. Overriding any of these does not fail;
#: it produces a run that works and reports the wrong thing, which is the failure a campaign
#: cannot see. ``SCENARIO_PARAMETER_FILE`` is the sharpest case: repointing it changes which
#: parameters the runner reads while every result still carries the configuration's name.
#:
#: **What deliberately does NOT belong here** is the handful of display and GPU hints a lane
#: arranges -- ``DISPLAY``, ``LIBGL_ALWAYS_SOFTWARE``, ``NVIDIA_*``, ``QT_X11_NO_MITSHM``.
#: Those steer how a container renders, not whether its results mean what they say, and a
#: campaign has legitimate reasons to set them.
#:
#: **Module level, not inline in the validator below**, because ``execution.env`` is no
#: longer the only way into a run's environment: the ``sut:`` channel's ``env`` carrier
#: reaches the same place by a different route, and a guard living inside one field's
#: validator would protect that field and silently not the other.
#:
#: This list is checked against the emitters by ``tests/common/test_reserved_env.py``, which
#: fails when a lane starts injecting a name that is not registered here. A hand-maintained
#: denylist goes stale silently -- it passes while it protects nothing -- and that test is
#: what stops this one from doing so.
RESERVED_ENV_NAMES = frozenset({
    # identity and the record
    'CAMPAIGN_ID',
    # where the run writes
    'OUTPUT_DIR', 'SCENARIO_OUTPUT_DIR', 'RUN_OUTPUT_DIR', 'OUTPUT_RESULT_PER_SCENARIO',
    # what the run executes, and with what
    'SCENARIO_FILE', 'SCENARIO_PARAMETER_FILE', 'SCENARIO_EXECUTION_PARAMETERS',
    'SCENARIO_MODE', 'SIMULATION', 'CONTAINER_NAME', 'ROBOVAST_CONTAINER_COMMAND',
    # hooks the campaign declares by their own keys, not by env
    'PRE_COMMAND', 'POST_COMMAND',
    # logging derived from the .vast
    'BT_LOG', 'LOG_TOPICS',
    # what the container is allowed to use
    'AVAILABLE_CPUS', 'AVAILABLE_MEM',
    # upload credentials and where a campaign's objects land
    'S3_ENDPOINT', 'S3_BUCKET', 'S3_ACCESS_KEY', 'S3_SECRET_KEY', 'S3_PREFIX',
    'S3_CAMPAIGN_PREFIX',
})


class ExecutionConfig(BaseModel):
    #: Every container this campaign runs, keyed by name -- the one namespace shared by
    #: the schema, ``exec_in_container`` and a scenario's ``remote()`` endpoints. Three
    #: names have a defined meaning (:data:`CONTAINER_ROLES`); anything else is an
    #: ad-hoc container. Replaces the former ``image`` / ``resources`` /
    #: ``secondary_containers`` / top-level ``build:``, which are gone in version 2.
    containers: dict[str, ContainerConfig]
    #: How each container's CPU reservation is decided.
    #:
    #: ``fixed`` (the default) takes the figure from ``containers.<name>.resources``, which
    #: is what a ``.vast`` has always meant.
    #:
    #: ``calibrated`` measures it instead: one probe run per node before the campaign places
    #: work there, then that node's jobs sized from what was measured on it. ``resources``
    #: keeps its meaning there rather than being refused -- it is where measuring starts and
    #: the ceiling a measured figure may not exceed -- so it has to sit ABOVE demand: a
    #: container capped at what it wants throttles against the cap, and its probe is refused
    #: as having measured the ceiling instead of the demand.
    #:
    #: The reason to prefer it is portability rather than density: a core count is a fact
    #: about the machine it was measured on, so a shipped ``.vast`` naming one asserts
    #: something it cannot know about the cluster it lands on.
    sizing: Optional[Literal["fixed", "calibrated"]] = None
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
    # Maximum wall-clock time in seconds for one JOB -- one unit of work, which is one run
    # unless ``runs_per_job`` packs several. A job is the granularity both lanes can
    # actually enforce at (a Job's activeDeadlineSeconds; a compose step), so the number is
    # used as declared rather than reconstructed from a per-run figure.
    timeout: Optional[int] = None
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
    # Size of the pod's shared ``/dev/shm``. One tmpfs is mounted into every container of
    # the run, which is what lets ROS 2's default Fast DDS use its shared-memory transport
    # across the scenario / sut / simulation boundary.
    #
    # Defaulted rather than left to the lanes, because the two lane defaults DISAGREE and
    # both are traps: the cluster lane's memory-backed ``emptyDir`` has no size limit, so
    # it is sized from the pod's memory limits -- or, with none declared, from the whole
    # node -- while the local lane inherits Docker's 64 MB. A container that overruns the
    # pool dies of SIGBUS (exit 135), not a clean OOM, so the death arrives with nothing
    # explaining it. One default here is what makes a `.vast` mean the same thing on both
    # lanes without every campaign having to say so.
    #
    # There is deliberately no way to ask for "the lane's own default": it is the thing
    # this default exists to avoid. A campaign that needs more says a bigger number, and
    # ``get_campaign_summary`` reports the measured peak to size it from.
    shm_size: str = DEFAULT_SHM_SIZE

    @model_validator(mode="after")
    def resolve_sizing(self):
        """Unset means *infer it from the file*, and the file already says.

        A campaign that declares ``resources`` has answered the question, so it is
        ``fixed``; one that declares none is asking to be measured, so it is ``calibrated``.
        The default is therefore calibrated for anything written from now on, without
        refusing every ``.vast`` that already carries a number -- which a flat default would,
        including archived campaigns being read back.

        Saying ``sizing:`` explicitly still wins, and is how a campaign asks for measured
        sizing while a declaration is still in the file (refused, below) or for declared
        sizing with nothing declared (refused at admission, which names the containers).
        """
        if self.sizing is None:
            self.sizing = infer_sizing(self)
        return self

    def _declares_resources(self) -> bool:
        """Whether any container states a cpu or memory figure. ``gpu`` does not count: it is
        a device count rather than a rate, so nothing measures it and it never conflicts."""
        return any(
            getattr(c, "resources", None) is not None
            and any(getattr(c.resources, f, None) is not None
                    for f in ("cpu", "cpu_limit", "memory", "memory_limit"))
            for c in (self.containers or {}).values())

    @model_validator(mode="after")
    def validate_calibration_is_reachable(self):
        """A ``calibration`` block under ``sizing: fixed`` decides nothing, and must say so.

        Nothing is measured in that mode, so every field in the block is inert. Silence would
        make a file read as configured while behaving as default -- the failure this whole
        area keeps producing. Raised rather than warned because it is unambiguous: the two
        keys are in the same file, and there is no reading of them under which the block does
        anything.
        """
        if self.sizing == "calibrated":
            return self
        named = sorted(name for name, c in (self.containers or {}).items()
                       if getattr(c, "calibration", None) is not None)
        if named and self.sizing == "fixed":
            raise ValueError(
                f"execution.sizing is 'fixed', so nothing is measured -- but "
                f"{', '.join(named)} declare execution.containers.<name>.calibration, which "
                f"decides how a measurement becomes an allocation and is therefore read only "
                f"under 'calibrated'. Set execution.sizing: calibrated to use it, or remove "
                f"the block.")
        return self

    @model_validator(mode="after")
    def validate_limit_rule_matches_the_ceiling(self):
        """``calibration.limit: request`` and an explicit ``resources.cpu_limit`` conflict.

        ``limit`` picks the rule and ``resources`` supplies the value it reads -- so with
        ``request`` the ceiling becomes the measured request and a declared ``cpu_limit`` is
        never used. Accepting both would leave a file stating a number nobody honours, which
        is the same defect as the pair this block already refuses for an unknown key.
        """
        named = []
        for name, container in (self.containers or {}).items():
            calibration = getattr(container, "calibration", None)
            resources = getattr(container, "resources", None)
            if (getattr(calibration, "limit", None) == "request"
                    and getattr(resources, "cpu_limit", None) is not None):
                named.append(name)
        if named:
            raise ValueError(
                f"{', '.join(sorted(named))} set calibration.limit: request, which makes the "
                f"limit equal the measured request -- and also declare resources.cpu_limit, "
                f"which is then never used. Remove the cpu_limit, or set "
                f"calibration.limit: declared to keep it as the ceiling.")
        return self

    @field_validator('shm_size')
    @classmethod
    def validate_shm_size(cls, v: str) -> str:
        """Reject a value that is not a memory quantity, here rather than at the lane.

        Both lanes pass this string through to a manifest untouched, so an unparseable one
        otherwise surfaces as a Kubernetes rejection or a ``docker compose`` error, minutes
        into a campaign and nowhere near the line that caused it.

        An explicit ``null`` is rejected along with the rest. It reads as "no opinion", but
        the thing it would ask for -- whatever each lane defaults to on its own -- is what
        this field exists to stop a campaign from getting by accident.
        """
        if to_bytes(v) is None:
            raise ValueError(
                f"shm_size {v!r} is not a memory quantity -- use a size like '512Mi' or "
                f"'2Gi', or omit it for the default of {DEFAULT_SHM_SIZE}")
        return v

    @field_validator('env')
    @classmethod
    def validate_no_reserved_env_vars(cls, v: Optional[list[dict[str, str]]]) -> Optional[list[dict[str, str]]]:
        """Refuse an ``env`` entry naming something RoboVAST sets itself.

        Refused at validation rather than left to the lanes, because whether a campaign's
        value actually displaces RoboVAST's depends on emission order and on each lane's
        duplicate-key semantics -- so "it happens not to win today" is not a property to
        rely on, and a campaign that quietly had no effect is indistinguishable from one
        that worked.
        """
        if v is None:
            return v

        reserved_keys = RESERVED_ENV_NAMES

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


def declared_job_seconds(execution_params: dict) -> Optional[int]:
    """``execution.timeout`` as declared: the budget for one **job**, or ``None``.

    A job is what either lane can actually bound -- the cluster sets
    ``activeDeadlineSeconds`` on the Job, and the local lane wraps a whole compose step --
    so this is the number they use unchanged. It is deliberately not scaled by
    ``runs_per_job``: a packed job's budget is the budget its author stated, not a per-run
    figure multiplied back up.
    """
    timeout = (execution_params or {}).get("timeout")
    return int(timeout) if timeout else None


def declared_per_run_seconds(execution_params: dict) -> Optional[int]:
    """The per-run share of the declared job budget, or ``None``.

    Reporting needs a per-run figure even though nothing can enforce one: ``stalled`` is a
    verdict about a run. With runs packed, the honest per-run share is the job's budget
    divided by how many runs are in it.

    Distinct from :func:`job_deadline_seconds`, and the distinction matters because the two
    answer different questions:

    * *Enforcement* ("never let a campaign hang forever") may fall back to a
      backstop, because killing a wedged run an hour late still beats never.
    * *Reporting* ("is this run broken?") may **not**. Judging a two-minute pilot
      against an hour-long backstop yields ``stalled: false`` for the first hour of a
      run that is already dead -- a health certificate for a corpse, which is worse
      than saying nothing. With no declared budget there is no honest threshold, so
      this returns ``None`` and the reader must decline to give a verdict.
    """
    declared = declared_job_seconds(execution_params)
    if declared is None:
        return None
    runs_per_job = (execution_params or {}).get("runs_per_job") or 1
    return max(1, declared // int(runs_per_job))


def job_deadline_seconds(execution_params: dict) -> int:
    """How long one job may take before it is force-killed, in seconds.

    The **enforcement** figure: the cluster backend sets it as a Job
    ``activeDeadlineSeconds`` so a scenario that never shuts itself down cannot hang
    the campaign forever. Falls back to a backstop, which is why it must not be used to
    *report* health -- see :func:`declared_per_run_seconds`.

    The backstop is per-run and scaled, where a declaration is not. That asymmetry is
    deliberate: :data:`DEFAULT_RUN_DEADLINE_SECONDS` is an hour chosen in ignorance of the
    campaign, so it has to grow with the number of runs packed behind it or a job of 100
    runs would be killed after the first few. A declared number is a statement about the
    job, and is taken at face value.

    Only the cluster lane enforces this; locally nothing does (see ``execute_local``,
    which says so in the generated ``run.sh``).
    """
    declared = declared_job_seconds(execution_params)
    if declared is not None:
        return declared
    runs_per_job = (execution_params or {}).get("runs_per_job") or 1
    return DEFAULT_RUN_DEADLINE_SECONDS * int(runs_per_job)


class ResultsConfig(BaseModel):
    #: What the postprocessing pod's conversion step may have, and how many bags it converts
    #: at once. Omitted, the step takes its built-in reservation -- see
    #: :class:`PostprocessResourcesConfig`, which documents why only this one step is
    #: settable and why the figure is a reservation rather than a ceiling.
    resources: Optional[PostprocessResourcesConfig] = None
    postprocessing: Optional[list[str | dict[str, Any]]] = None
    metadata_processing: Optional[list[str | dict[str, Any]]] = None
    publication: Optional[list[str | dict[str, Any]]] = None
    #: Per-run health checks to run, graded into the ``run_health`` table. Either an
    #: installed ``robovast.health_checks`` plugin by name (``nav2_control_loop_rate``) or a
    #: local ``./path.py:Class`` ref, which is how a system under test ships a check without
    #: packaging it.
    #:
    #: A check is called ``check(conn, campaign_id)`` against the central index and must
    #: scope every statement it issues to that campaign -- one set of tables holds the whole
    #: corpus. The argument is required, so a check written for the old ``check(conn)``
    #: signature is refused rather than left to grade every campaign at once.
    #:
    #: **Nothing runs undeclared.** A check that ran everywhere would grade campaigns it
    #: knows nothing about: nav2's control-loop check finds no misses in a MoveIt 2 campaign
    #: and would write ``ok`` for every run of it -- a clean bill for a stack that was never
    #: there. Declaring is also what makes the campaign record say which checks were *meant*
    #: to run, so a missing row cannot be confused with a plugin that was not installed on
    #: whichever machine did the postprocessing.
    health_checks: Optional[list[str | dict[str, Any]]] = None


class PlotSpec(BaseModel):
    """A user-declared eval plot: a read-only SQL query + a Vega-Lite encoding.

    The query runs over the campaign's rows in the results index (``runs`` + metric tables,
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

#: Run-view panels every campaign gets, whatever its ``.vast`` declares and whatever simulator it
#: runs. The transport bar is not a visualization choice: without it a run view has no clock to
#: scrub and every other panel has nothing to follow, so there is nothing for a ``.vast`` to decide
#: and nothing it should have to write. Contributed by :func:`~robovast.common.simulators.merge_default_panels`
#: alongside the configured backend's own panels, and deduplicated the same way -- a campaign that
#: declares ``playback`` itself keeps its own entry, so moving or re-titling the bar still works.
ALWAYS_ON_PANELS = [{"playback": {}}]

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


def always_on_panel_types() -> frozenset:
    """The panel *types* of :data:`ALWAYS_ON_PANELS`.

    These are the panels that are not content: contributed to every run view whatever the
    ``.vast`` says, so a view holding nothing but these is a view with nothing to look at --
    which is what the web run-view asks in order to offer help authoring panels. Read through
    :func:`flatten_panel_shorthand`, so "which type is this entry" cannot come to mean one
    thing here and another where the list is merged or served.
    """
    return frozenset(flatten_panel_shorthand(p).get("type") for p in ALWAYS_ON_PANELS)


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

#: The panel type that renders an author-supplied Vega-Lite spec over a results table. Its
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
        failure mode: an unknown key that validates cleanly and draws nothing leaves an empty panel
        in the browser as its only symptom, with nothing naming the key that was ignored.
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
    the ``.vast``. The panel's own data bindings (e.g. ``layers``/``source`` naming results
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
                "a 'vega' panel must set 'source' to a results table, e.g. "
                "source: {table: poses, filter: {frame: base_link}}")
        for _field, message in panel_source_problems(extra):
            raise ValueError(message)
        return self


class TimelineConfig(BaseModel):
    """Which results table + column defines the run's playback timeline. Set this
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
    snapshot ``.vast``; each panel reads existing results tables."""
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


#: The tree levels an Explorer notebook can be declared for, outermost last. ``batch`` is a
#: *logical* level with no directory of its own, and appears in the tree only for a search
#: campaign. Lives here rather than in the service because it is part of what a ``.vast`` may
#: say: the renderer keeps only the scopes it recognises, so an unchecked misspelling drops the
#: notebook with nothing said anywhere.
EXPLORER_SCOPES = ("run", "config", "batch", "campaign")


class ExplorerConfig(BaseModel):
    """The Results **Explorer**: analysis notebooks, executed server-side per selected tree
    node (campaign / batch / config / run) and rendered as HTML."""
    model_config = ConfigDict(extra='forbid')
    notebooks: Optional[list[dict[str, Any]]] = None

    #: The Explorer appends a built-in **Log** tab after the declared workloads, at run level.
    #: It is not declarable -- a run always has a log -- so a workload of the same name renders a
    #: second tab that reads the same, and neither says which is which. Refused here rather than
    #: renamed or disambiguated downstream: the tab bar is what is wrong, and it is also what the
    #: URL addresses a tab by (``?tab=log``), so a name nobody can tell apart would be a link
    #: nobody can resolve.
    RESERVED_WORKLOAD_NAMES: ClassVar[tuple[str, ...]] = ('log',)

    @model_validator(mode='after')
    def _no_reserved_workload_name(self):
        for view in (self.notebooks or []):
            if not isinstance(view, dict):
                continue
            for name in view:
                if str(name).lower() in self.RESERVED_WORKLOAD_NAMES:
                    raise ValueError(
                        f"notebook workload '{name}' uses a reserved name: the Explorer already "
                        f"shows a built-in Log tab for every run, so this would add a second tab "
                        f"reading the same. Rename the workload."
                    )
        return self

    @model_validator(mode='after')
    def _known_scopes_only(self):
        """Every scope key must be one the Explorer can address.

        The renderer selects the scopes it knows and ignores the rest, so a typo -- or a scope
        renamed since the ``.vast`` was written, as ``single_test`` was -- left the notebook
        declared, staged, and never rendered, with no tab and no message to say why.
        """
        for view in (self.notebooks or []):
            if not isinstance(view, dict):
                continue
            for name, scopes in view.items():
                if not isinstance(scopes, dict):
                    continue
                unknown = [s for s in scopes if s not in EXPLORER_SCOPES]
                if unknown:
                    raise ValueError(
                        f"notebook workload '{name}' declares unknown scope(s) "
                        f"{', '.join(repr(u) for u in unknown)}. The Explorer addresses "
                        f"{', '.join(EXPLORER_SCOPES)}; a notebook under any other key is "
                        f"never rendered."
                    )
        return self


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


class RepetitionsConfig(BaseModel):
    """How many times each parameter set is evaluated — a policy, not a constant.

    ``execution.runs`` gives every cell the same number of repetitions. That is the
    right default and the wrong one in the same campaign: a cell whose runs all agree
    was decided by the first one, while a cell on a failure boundary is exactly where
    more samples buy something. Measured on a quadrotor search: 3 of 32 configurations
    produced a mixed outcome over 5 repetitions, so 145 of 160 runs bought one bit each.

    This is a **policy layer, not a strategy**: it is applied between ``ask()`` and
    composition, so it composes with every strategy instead of being one of them. A
    strategy that is noise-aware may still speak for itself -- a ``ParamSet`` that
    already carries ``n_reps`` is left alone.

    ``adaptive`` allocates from *local disagreement*: a candidate whose nearest already
    evaluated neighbours agree gets ``min``, one sitting where they disagree gets up to
    ``max``. Objective-agnostic on purpose -- it reads spread, not a threshold, so it
    works for a rate, a margin or a time without being told which it is.
    """
    model_config = ConfigDict(extra='forbid')
    policy: Literal['fixed', 'adaptive'] = 'fixed'
    min: int = 1
    max: int = 1
    #: Neighbours consulted when judging local disagreement (``adaptive`` only).
    neighbours: int = 5
    #: Reuse one seed list across every cell, so two cells are compared run-for-run
    #: instead of only in distribution. Pairing covers the SIMULATOR's seeded noise
    #: only -- a stack running asynchronously in its own container is not replayable --
    #: so it reduces variance without making a single run reproducible.
    paired: bool = False
    #: Where the per-repetition seed is delivered, as a variation channel mapping
    #: (e.g. ``{sim: seed}``). Absent means repetitions stay unseeded: they still
    #: differ, but neither pairing nor replay is possible.
    seed_parameter: Optional[dict] = None

    @field_validator('min', 'max', 'neighbours')
    @classmethod
    def _positive(cls, v: int, info) -> int:
        if v < 1:
            raise ValueError(f"repetitions {info.field_name} must be >= 1, got {v}")
        return v

    @model_validator(mode='after')
    def _ordered(self):
        if self.max < self.min:
            raise ValueError(
                f"repetitions max ({self.max}) must be >= min ({self.min})")
        if self.seed_parameter is not None or self.paired:
            # Refused rather than accepted-and-ignored. Pairing needs repetition i of every cell
            # to draw the same noise, and neither channel available today delivers that:
            #
            #   - a simulator override document is written per CONFIG, so every repetition of a
            #     cell would read one seed and stop varying -- strictly worse than the present
            #     behaviour, where an unseeded run draws its own;
            #   - the simulator's own episode counter cannot stand in for it, because jobs are
            #     packed by simulator settings rather than by configuration (see
            #     execution/packer.py: FixedK groups on WorkItem.sim_key), so one process's
            #     episodes run across several cells and "episode i" is not "repetition i".
            #
            # What it needs is a per-run seed on the execution backend. Until that exists, saying
            # 'paired' would claim a comparison the data cannot support.
            raise ValueError(
                "repetitions 'paired'/'seed_parameter' need a per-run seed, which no execution "
                "backend delivers yet: a simulator override document is written per configuration, "
                "so every repetition of a cell would receive the SAME seed and stop varying. Drop "
                "them -- the allocation half works unseeded (repetitions still vary; they simply "
                "cannot be paired or replayed).")
        return self


class EvaluationsBudget(BaseModel):
    """Resource cap: stop after this many parameter sets have been SCORED.

    Counts evaluations, not executions -- a cell evaluated once may have cost many
    repetitions. Use :class:`RunsBudget` to bound wall-clock.
    """
    model_config = ConfigDict(extra='forbid')
    type: Literal['evaluations']
    value: int

    @field_validator('value')
    @classmethod
    def _positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"budget evaluations value must be >= 1, got {v}")
        return v


class RunsBudget(BaseModel):
    """Resource cap: stop after this many individual RUNS have executed.

    The cap that actually bounds wall-clock. ``batches x per_batch x execution.runs``
    predicts the run count only while every cell gets the same number of repetitions;
    once ``search.repetitions`` makes that adaptive it predicts nothing, which is
    exactly when a run cap is needed. It is also what makes two strategies comparable:
    a fair contest gives both the same number of executions, not the same number of
    batches.
    """
    model_config = ConfigDict(extra='forbid')
    type: Literal['runs']
    value: int

    @field_validator('value')
    @classmethod
    def _positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"budget runs value must be >= 1, got {v}")
        return v


# A resource cap; the search stops when ANY budget criterion is hit.
BudgetCriterion = Annotated[
    Union[BatchesBudget, TimeBudget, EvaluationsBudget, RunsBudget],
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
_BUDGET_SCALAR = {'batches': 'value', 'time': 'seconds',
                  'evaluations': 'value', 'runs': 'value'}
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
    #: Repetition allocation policy; absent keeps `execution.runs` for every cell.
    repetitions: Optional[RepetitionsConfig] = None
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
            "private repo, provide a GitHub token at 'vast cluster setup'); or a "
            "workspace-relative path to a wheel you uploaded "
            "('./plugins/my_plugin-1.0-py3-none-any.whl'). They are installed into the "
            "'.robovast_plugins/' venv (with dependencies) and put on sys.path before "
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
        # A bare local path only resolves where the workspace is, so entries
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
#: Not the recovery path -- ``robovast.common.migrations`` converts a v1 config. This text
#: exists because it explains the restructuring in one screen, which a caller staring at a
#: refusal wants. ``migrations/config/v1_to_v2.py`` is the executable form and must agree
#: with it. Keep each entry as "what you wrote" -> "what it becomes".
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
