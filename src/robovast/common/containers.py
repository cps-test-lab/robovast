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

"""Turn ``execution.containers`` into the containers a run actually starts.

**One map, built once.** Compose generation, the Kubernetes job manifest, the image
build, ``exec_in_container`` and a scenario's ``remote()`` endpoints all address
containers by name, and they must agree about what those names mean. A second lookup
somewhere else would be free to disagree with what actually runs, and the disagreement
would be silent -- a diagnostic entering one container while the campaign ran another.

A *role* is not a container count. ``scenario``, ``simulation`` and ``sut`` are always
valid names to ask about; how many real containers back them depends on the campaign:

* three, when the simulator and the system under test each get their own;
* two, when the simulator is stepped in-process by scenario-execution (``simulation``
  resolves to the ``scenario`` container);
* one, for a campaign with no simulator and no separate stack.

Only :data:`~robovast.common.config.SCENARIO_CONTAINER` is guaranteed to exist -- it is
the container that runs the scenario, and every campaign has one.
"""

from dataclasses import dataclass, field
from typing import Optional

from robovast.common.config import (CONTAINER_ROLES, SCENARIO_CONTAINER,
                                    SIMULATION_CONTAINER, SUT_CONTAINER)

__all__ = [
    'ContainerPlan',
    'PlannedContainer',
    'plan_containers',
]


@dataclass(frozen=True)
class PlannedContainer:
    """One container that will actually be started."""

    #: Its name in the pod / compose project, and the name every tool addresses it by.
    name: str
    #: What it starts from. When :attr:`builds` is set this is the *base*, and the
    #: resolved image is substituted once the build has run.
    image: Optional[str]
    #: What it runs. ``None`` means the default for its position: the scenario runner
    #: for the main container, the scenario-execution server for a sidecar (which is
    #: what makes it drivable from a scenario with ``remote()``).
    command: Optional[list] = None
    resources: dict = field(default_factory=dict)
    #: The roles this container backs -- usually one, but a stepped simulator makes the
    #: scenario container answer to ``simulation`` as well.
    roles: tuple = ()
    #: apt packages added on top of :attr:`image`. When one container backs several
    #: roles these are merged from every block that describes it, so extending
    #: ``simulation`` reaches the scenario container when the simulator is stepped
    #: in-process -- otherwise a campaign's own simulator plugins would be silently
    #: dropped for exactly the shape that needs them most.
    system_packages: tuple = ()
    #: Python install groups added on top of :attr:`image`, merged the same way.
    python_packages: tuple = ()

    @property
    def builds(self) -> bool:
        """Whether an image is built on top of :attr:`image` for this container."""
        return bool(self.system_packages or self.python_packages)

    @property
    def is_main(self) -> bool:
        """Whether this is the container scenario-execution runs in."""
        return SCENARIO_CONTAINER in self.roles


@dataclass(frozen=True)
class ContainerPlan:
    """Every container of a run, plus the role→name map."""

    #: Start order, main container first.
    containers: tuple
    #: role name -> container name. Only roles this campaign actually has.
    roles: dict

    @property
    def main(self) -> PlannedContainer:
        return self.containers[0]

    @property
    def sidecars(self) -> list:
        return [c for c in self.containers[1:]]

    def by_name(self, name: str) -> PlannedContainer:
        """Resolve a container *or* role name to the container that backs it.

        Raises ``KeyError`` with the available names, because the caller is usually a
        person or an agent choosing one -- "no such container" without the list is a
        second round trip.
        """
        resolved = self.roles.get(name, name)
        for container in self.containers:
            if container.name == resolved:
                return container
        known = sorted({c.name for c in self.containers} | set(self.roles))
        raise KeyError(
            f"no container '{name}' in this campaign; it has: {', '.join(known)}")

    def names(self) -> list:
        return [c.name for c in self.containers]


def plan_containers(execution: dict, *, images: Optional[dict] = None,
                    explicit_main: Optional[str] = None,
                    main_image_fallback: Optional[str] = None) -> ContainerPlan:
    """Build the plan from a campaign's ``execution`` section.

    *execution* is the raw mapping the lanes carry (``campaign_data["execution"]``),
    not the pydantic model, because that is what both lanes have in hand.

    Image resolution, highest precedence first, per container:

    1. *explicit_main* -- a ``--image`` flag, which addresses the main container only;
    2. *images* -- concrete refs produced by the build lifecycle, keyed by container
       name, for containers that build one;
    3. the container's declared ``image``;
    4. *main_image_fallback* -- ``ROBOVAST_IMAGE`` / the built-in default, main
       container only. A sidecar has no such fallback: guessing an image for the
       system under test would run something nobody named.

    Passing none of the optional arguments gives the *planned* shape, with declared
    images and no substitution -- which is what validation and documentation want.
    """
    # Absent means the scenario container and nothing else. The schema requires
    # ``containers`` where a campaign is *authored*; this also runs against raw
    # ``campaign_data`` assembled by hand (offline manifest emit, tests),
    # where an explicit image is passed instead — refusing that would turn a legitimate
    # one-container run into an error about a key it never needed.
    declared = execution.get('containers') or {}

    def _block(name):
        block = declared.get(name)
        return block if isinstance(block, dict) else None

    scenario = _block(SCENARIO_CONTAINER) or {}
    simulation = _block(SIMULATION_CONTAINER)

    # A simulator gets its own container unless it is stepped in-process, in which case
    # it *is* the scenario container. Phase 1 knows only the explicit form (an image, or
    # a command to run); a backend fills the same fields in before we get here.
    simulation_is_separate = bool(
        simulation and (simulation.get('image') or simulation.get('command')))

    roles = {SCENARIO_CONTAINER: SCENARIO_CONTAINER}
    folded = simulation is not None and not simulation_is_separate
    # A stepped simulator IS the scenario container, so both blocks describe it.
    scenario_blocks = [scenario] + ([simulation] if folded else [])
    containers = [PlannedContainer(
        name=SCENARIO_CONTAINER,
        image=next((b.get('image') for b in scenario_blocks if b.get('image')), None),
        command=next((b.get('command') for b in scenario_blocks if b.get('command')), None),
        resources=scenario.get('resources') or {},
        roles=(SCENARIO_CONTAINER, SIMULATION_CONTAINER) if folded else (SCENARIO_CONTAINER,),
        system_packages=_merged(scenario_blocks, 'system_packages'),
        python_packages=_merged(scenario_blocks, 'python_packages'),
    )]
    if folded:
        # The name still resolves -- to the container the simulator runs in.
        roles[SIMULATION_CONTAINER] = SCENARIO_CONTAINER

    for name, block in declared.items():
        if name == SCENARIO_CONTAINER:
            continue
        if name == SIMULATION_CONTAINER and folded:
            continue
        block = block or {}
        containers.append(PlannedContainer(
            name=name,
            image=block.get('image'),
            command=block.get('command'),
            resources=block.get('resources') or {},
            roles=(name,) if name in CONTAINER_ROLES else (),
            system_packages=_merged([block], 'system_packages'),
            python_packages=_merged([block], 'python_packages'),
        ))
        if name in CONTAINER_ROLES:
            roles[name] = name

    # A stack that bundles its own simulator: nothing declares a simulation container,
    # so asking for one should reach the sut rather than fail.
    if SIMULATION_CONTAINER not in roles and SUT_CONTAINER in roles:
        roles[SIMULATION_CONTAINER] = roles[SUT_CONTAINER]

    containers = [_resolve_image(c, images or {}, explicit_main, main_image_fallback)
                  for c in containers]
    return ContainerPlan(containers=tuple(containers), roles=roles)


def _resolve_image(container: PlannedContainer, images: dict,
                   explicit_main: Optional[str],
                   main_image_fallback: Optional[str]) -> PlannedContainer:
    """Substitute the concrete image for one container (see :func:`plan_containers`)."""
    if container.is_main and explicit_main:
        resolved = explicit_main
    elif container.name in images:
        resolved = images[container.name]
    elif container.image:
        resolved = container.image
    elif container.is_main and main_image_fallback:
        resolved = main_image_fallback
    elif images or explicit_main or main_image_fallback:
        # Only complain once resolution was actually asked for; the planning-only call
        # legitimately leaves a backend-supplied image unset.
        raise ValueError(
            f"no image for container '{container.name}': set "
            f"execution.containers.{container.name}.image"
            + (", or declare a simulator backend that supplies one"
               if container.is_main else ""))
    else:
        return container
    from dataclasses import replace
    return replace(container, image=resolved)


def _merged(blocks: list, key: str) -> tuple:
    """Concatenate one package list across every block describing a container.

    Order is block order, which is install order: the scenario block's packages go in
    before the simulation block's, so a simulator plugin can depend on something the
    campaign put underneath it.
    """
    out = []
    for block in blocks:
        out.extend(block.get(key) or [])
    return tuple(out)
