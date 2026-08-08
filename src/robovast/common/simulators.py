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

"""Simulator backends: what RoboVAST knows about a simulator, which is a *shape*.

A campaign names a backend in its ``simulation`` container and the backend fills in what
would otherwise be restated by hand in every ``.vast`` -- the image, the packages it
needs, the environment it reads, how it is started. RoboVAST never names a simulator:
backends are resolved from the ``robovast.simulators`` entry-point group, exactly as
variation types and panel types are.

**Two shapes**, and a backend declares which it serves:

``stepped``
    scenario-execution owns the loop and calls ``step()`` -- so the simulator runs
    *in the scenario's process*, and the two roles resolve to one container. Exactly
    reproducible, because time advances only when the behaviour tree ticks.

``ros``
    the simulator runs on its own, publishes ``/clock``, and the scenario observes it
    over ROS -- so it gets its own container. No ``SimulationInterface`` is involved,
    which is why a simulator that has none (Gazebo, Isaac) fits here unchanged.

**A backend must import without its simulator installed.** It is imported in the
long-lived service process, which has no reason to carry a MuJoCo or an Isaac runtime;
it declares strings and container specs, and anything needing the simulator itself runs
*in the simulator's image* (see :meth:`SimulatorBackend.input_files`).
"""

from __future__ import annotations

from typing import Optional

from robovast.common.config import (SCENARIO_CONTAINER, SIMULATION_CONTAINER,
                                    SUT_CONTAINER)

#: Entry-point group backends register in.
SIMULATOR_GROUP = "robovast.simulators"

#: The simulator is stepped in-process by scenario-execution (``mode: base``).
SHAPE_STEPPED = "stepped"
#: The simulator runs on its own and publishes ``/clock`` (``mode: ros2``).
SHAPE_ROS = "ros"


def shape_for(mode: str) -> str:
    """Which shape a campaign's ``execution.mode`` implies.

    ``mode`` already answers "does the trial speak ROS?", and that is the same question
    as "can the simulator run in its own container?" -- so the shape is derived rather
    than being a second key an author has to keep consistent with the first.
    """
    return SHAPE_ROS if mode == "ros2" else SHAPE_STEPPED


class SimulatorBackend:
    """What a simulator tells RoboVAST about itself.

    Every hook receives the validated ``simulation`` container block (``cfg``, its own
    :attr:`CONFIG_CLASS` instance) and the resolved ``execution`` mapping, so a backend
    can key on ``mode`` -- which is the only thing that differs between the two shapes
    for most simulators.

    Defaults are the "nothing to add" answers, so a backend implements only what it
    actually has to say.
    """

    #: A pydantic model for this backend's own keys. RoboVAST validates ``backend`` and
    #: hands the rest here, so adding a key to a simulator is not a RoboVAST change.
    CONFIG_CLASS = None

    #: Which of :data:`SHAPE_STEPPED` / :data:`SHAPE_ROS` this backend serves. A request
    #: for another is refused at validation time, naming what is supported -- the
    #: "capability is declared by the plugin, never listed in core" rule.
    SUPPORTED_SHAPES: tuple = (SHAPE_STEPPED, SHAPE_ROS)

    def containers(self, cfg, execution: dict) -> dict:
        """Container blocks this backend contributes, keyed by container name.

        Merged *underneath* what the campaign declared, so an author always wins. This
        is where a backend supplies its default image, and -- in the ``ros`` shape --
        the command that starts the simulator in its own container.
        """
        return {}

    def simulation_ref(self, cfg, execution: dict) -> Optional[str]:
        """``module:Class`` of the ``SimulationInterface``, for the stepped shape.

        ``None`` in the ``ros`` shape, and for any simulator that has no such interface
        at all -- the hook is simply not called there.
        """
        return None

    def env(self, cfg, execution: dict) -> dict:
        """Environment the simulator reads, merged into every container.

        A campaign's own ``execution.env`` wins over this: these are defaults a backend
        knows, not decisions it takes away.
        """
        return {}

    def input_files(self, cfg, execution: dict) -> list:
        """Files the simulator needs that the campaign owns, relative to the ``.vast``.

        For robosito: a world declared as a path rather than a package ref. A packaged
        world travels inside the image and needs nothing here, which is the default.

        RoboVAST adds these to the campaign's ``run_files``, so each is mounted at
        ``/config/<path>`` where the simulator opens it, archived into
        ``<campaign>/_config/`` where the run view rebuilds geometry from it, and hashed
        into the configuration identity -- a changed world is a changed experiment.

        Declared here rather than written by the campaign because a ``.vast`` naming its
        world under ``config:`` and again under ``run_files:`` states one fact twice, and
        forgetting the second fails far from the cause: the simulator cannot open a path
        that was never mounted.

        Only the files the campaign itself owns. A world that ``extends`` another
        *campaign* file is not followed -- enumerating that needs the simulator, which
        must not be imported here. Such a run fails loudly in the container on a world it
        cannot resolve, rather than silently rendering the wrong one.
        """
        return []

    def produces_run_capture(self, cfg, execution: dict) -> bool:
        """Whether runs write the capture a ``scene3d`` panel replays.

        Replaces sniffing a campaign's wheel names for ``rst``: a capability question
        the simulator can answer, asked of whichever simulator is actually configured.
        """
        return False


def resolve_backend(name: str, base_dir: str = "") -> SimulatorBackend:
    """Load a backend by entry-point name, or by ``.vast``-relative ``<file>.py:<Class>``.

    The file form is the escape hatch for a campaign whose service environment does not
    have the backend installed: a descriptor next to the ``.vast`` works without any
    deployment step. Same resolution as variation plugins (``plugin_ref.load_ref``), so
    there is one spelling to learn.
    """
    from robovast.common.plugin_ref import load_ref
    loaded = load_ref(name, SIMULATOR_GROUP, base_dir)
    backend = loaded() if isinstance(loaded, type) else loaded
    if not isinstance(backend, SimulatorBackend):
        raise ValueError(
            f"simulator backend '{name}' is a {type(backend).__name__}, not a "
            "SimulatorBackend; it must subclass "
            "robovast.common.simulators.SimulatorBackend")
    return backend


def backend_name(execution: dict) -> Optional[str]:
    """The backend a campaign's ``execution`` names, if any."""
    containers = execution.get("containers") or {}
    block = containers.get(SIMULATION_CONTAINER)
    return (block or {}).get("backend") if isinstance(block, dict) else None


def apply_backend(execution: dict, base_dir: str = "") -> dict:
    """Return *execution* with its backend's contributions merged in.

    Called once, where the raw ``execution`` mapping is turned into what the lanes read,
    so every consumer downstream -- the container plan, the image builds, the env --
    sees one already-complete picture rather than each re-asking the backend.

    The campaign always wins: a backend supplies defaults for keys the author left out,
    and never overrides one they set.
    """
    name = backend_name(execution)
    if not name:
        return execution
    backend = resolve_backend(name, base_dir)
    shape = shape_for(execution.get("mode", "auto"))
    if shape not in backend.SUPPORTED_SHAPES:
        raise ValueError(
            f"simulator backend '{name}' does not support the "
            f"{'ROS' if shape == SHAPE_ROS else 'stepped'} shape that execution.mode "
            f"'{execution.get('mode')}' implies; it supports: "
            + ", ".join(sorted(backend.SUPPORTED_SHAPES)))

    containers = {name_: dict(block or {})
                  for name_, block in (execution.get("containers") or {}).items()}
    cfg = _validated_cfg(backend, containers.get(SIMULATION_CONTAINER) or {}, name)

    if shape == SHAPE_STEPPED:
        # A stepped simulator IS the scenario container -- so collapse the two blocks
        # here, where the shape is known, rather than leaving the container planner to
        # infer it. Container-level keys move to ``scenario``; ``simulation`` keeps only
        # ``backend`` and the backend's own keys, which is what marks it as folded.
        #
        # The author's keys move *first*, so a backend default cannot outrank an image
        # set on the ``simulation`` block -- the block they would naturally reach for,
        # since that is where they named the backend in the first place.
        sim_block = containers.get(SIMULATION_CONTAINER) or {}
        scenario_block = containers.setdefault(SCENARIO_CONTAINER, {})
        for key in _CONTAINER_KEYS:
            if sim_block.get(key) is not None:
                _set_if_unset(scenario_block, key, sim_block.pop(key))

    for target, defaults in backend.containers(cfg, execution).items():
        block = containers.setdefault(target, {})
        for key, value in (defaults or {}).items():
            _set_if_unset(block, key, value)

    result = dict(execution)
    result["containers"] = containers
    if shape == SHAPE_STEPPED and not result.get("simulation"):
        ref = backend.simulation_ref(cfg, execution)
        if ref:
            result["simulation"] = ref
    contributed = backend.env(cfg, execution)
    if contributed:
        result["_backend_env"] = contributed
    return result


def _set_if_unset(block: dict, key: str, value) -> None:
    """Fill ``key`` when the block does not really carry a value for it.

    Not ``setdefault``: an unset optional field survives ``model_dump()`` as an explicit
    ``None``, so the key is *present* and ``setdefault`` declines -- and every backend
    default is silently dropped. Only callers holding a validated config hit that (the
    run path passes the raw YAML, where an unset key is simply absent), which is how a
    build could be planned against ``image: None`` while the run used the backend's
    image. Treating ``None`` as "unset" makes both callers agree.
    """
    if block.get(key) is None:
        block[key] = value


def _validated_cfg(backend: SimulatorBackend, block: dict, name: str):
    """Validate the backend's own keys against its ``CONFIG_CLASS``.

    Without one the block is handed over as-is: a backend with no keys of its own should
    not have to declare an empty model to say so.
    """
    own = {k: v for k, v in block.items() if k not in _ROBOVAST_KEYS}
    if backend.CONFIG_CLASS is None:
        return own
    try:
        return backend.CONFIG_CLASS(**own)
    except Exception as err:  # noqa: BLE001 - re-raised naming the backend
        raise ValueError(
            f"execution.containers.{SIMULATION_CONTAINER} is not valid for simulator "
            f"backend '{name}': {err}") from None


#: Keys that describe the *container* rather than the simulator -- what moves to the
#: scenario block when a stepped simulator folds into it.
_CONTAINER_KEYS = ("image", "command", "resources", "system_packages", "python_packages")

#: Keys of the ``simulation`` block that belong to RoboVAST, not to the backend. A
#: backend's CONFIG_CLASS forbids extras, so offering it these would reject every
#: campaign that set one.
_ROBOVAST_KEYS = frozenset({"backend", *_CONTAINER_KEYS})

__all__ = [
    "SHAPE_ROS",
    "SHAPE_STEPPED",
    "SIMULATOR_GROUP",
    "SCENARIO_CONTAINER",
    "SIMULATION_CONTAINER",
    "SUT_CONTAINER",
    "SimulatorBackend",
    "apply_backend",
    "backend_name",
    "resolve_backend",
    "shape_for",
]
