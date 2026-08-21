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

import contextvars
import copy
import fnmatch
import json
import logging
import os
import re
import ssl
import subprocess  # nosec B404 - spawns the trusted robovast compose worker
import sys
import tarfile
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from importlib.metadata import entry_points
from pprint import pformat

from .common import convert_dataclasses_to_dict, get_scenario_parameters, load_config
from .config_identifier import collect_paths_from_config, hash_variation_entrypoints
from .config_plugins import ensure_workspace_plugins
from .errors import missing_input_error
from .file_cache2 import CacheKey, FileCache2
from .input_generation import (collect_output_files, parse_generate_entry, resolve_out_dir,
                               run_input_generators)
from .plugin_ref import is_file_ref, load_ref
from .variation.base_variation import VariationInfeasibleError
from .variation.loader import _validate_variation_class

logger = logging.getLogger(__name__)

_ssl_state = {"configured": False}


def _maybe_disable_ssl_verification():
    """Optionally skip TLS certificate verification for remote fetches.

    Some variations (e.g. ``SemanticGeneration``) parse JSON-LD whose
    ``@context`` URLs are dereferenced over HTTPS. When such a host serves a
    certificate that does not match its hostname, the fetch fails with
    ``CERTIFICATE_VERIFY_FAILED``. Setting ``ROBOVAST_INSECURE_SSL=1`` installs
    an HTTPS opener that skips certificate verification so generation can
    continue despite a broken certificate on the (trusted) data host.
    """
    if _ssl_state["configured"]:
        return
    if os.environ.get("ROBOVAST_INSECURE_SSL", "").strip().lower() not in (
        "1", "true", "yes", "on",
    ):
        return

    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    # ``urllib.request.urlopen`` caches a module-global opener on its first
    # no-context call, so a verifying request that already ran cannot be
    # overridden by patching the default context factory. Installing an opener
    # forces the unverified context for all subsequent fetches (rdflib's
    # JSON-LD loader goes through this same ``urlopen``).
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=context)
    )
    urllib.request.install_opener(opener)
    _ssl_state["configured"] = True
    logger.warning(
        "ROBOVAST_INSECURE_SSL is set: TLS certificate verification is "
        "DISABLED for remote fetches. Use only with trusted hosts."
    )


def progress_update(msg):
    logger.info(msg)


# Factory that turns a variation's ContainerSpec into a concrete ContainerRunner
# for the active execution backend. The service's per-campaign worker registers a
# cluster (aux-pod + pods/exec) factory via set_container_runner_factory(); when
# unset we fall back to the local (ephemeral ``docker run``) runner. See
# variation/container_runner.py.
#
# It is a **ContextVar, not a module global**, because the service now drives many
# campaigns concurrently as threads in one process (the driver used to be an
# isolated per-campaign pod). A new thread starts with a fresh context, so each
# worker's set() is scoped to that worker's composition — concurrent campaigns
# never clobber each other's aux-pod target. Composition is synchronous within the
# worker thread (no thread pool), so the value is visible where it's read.
_container_runner_factory: "contextvars.ContextVar" = contextvars.ContextVar(
    "robovast_container_runner_factory", default=None)


def set_container_runner_factory(factory):
    """Register the backend's ContainerRunner factory for the current context.

    Scoped to the calling thread/context (see the ContextVar note above); pass
    ``None`` to reset. Returns the ``Token`` so a caller may restore the prior
    value in a ``finally`` if it wants to.
    """
    return _container_runner_factory.set(factory)


#: Set in the child environment of the isolated compose subprocess. Its presence
#: makes ``generate_scenario_variations`` run the composition in-process (no further
#: fork) — see ``_compose_isolated`` and the dispatch in that function.
_ISOLATED_ENV = "ROBOVAST_ISOLATED_COMPOSE"

#: Set in the isolated worker's env only when the *parent* had a backend
#: ContainerRunner factory active (a cluster run). The factory is a ContextVar that
#: cannot cross the process boundary, so an aux-container variation cannot be built
#: in the worker; this flag lets ``_make_container_runner`` raise a clear error
#: instead of silently falling back to a local ``docker run`` that a cluster
#: controller pod cannot provide.
_ISOLATED_NO_BACKEND = "ROBOVAST_ISOLATED_NO_BACKEND"


def _make_container_runner(spec):
    """Build a runner for *spec* using the active factory (local fallback)."""
    if spec is None:
        return None
    if os.environ.get(_ISOLATED_NO_BACKEND) == "1" and _container_runner_factory.get() is None:
        # Isolated compose subprocess of a cluster run: the backend's runner factory
        # (a ContextVar) did not cross the process boundary, and a local docker
        # runner is not available in a controller pod. Fail clearly rather than
        # obscurely. Full support (a serializable runner descriptor handed to the
        # worker) is a documented follow-up.
        raise RuntimeError(
            "A variation requires an auxiliary container during composition, which "
            "is not yet supported under isolated plugin composition on a cluster "
            "backend. Run this campaign locally, or remove the aux-container "
            "variation from the plugin composition.")
    factory = _container_runner_factory.get()
    if factory is not None:
        return factory(spec)
    from .variation.container_runner import \
        LocalContainerRunner  # pylint: disable=import-outside-toplevel
    return LocalContainerRunner(spec)


def execute_variation(base_dir, configs, variation_class, parameters, general_parameters, progress_update_callback, scenario_file, output_dir=None, container_runner=None):
    logger.debug(f"Executing variation: {variation_class.__name__}")
    variation = variation_class(base_dir, parameters, general_parameters, progress_update_callback, scenario_file, output_dir, container_runner=container_runner)

    # Collect input files for campaign self-containment
    input_files = variation.get_input_files()

    try:
        configs = variation.variation(copy.deepcopy(configs))
    except VariationInfeasibleError as e:
        msg = f"Variation failed. {variation_class.__name__}: {e}"
        logger.error(msg)
        progress_update_callback(msg)
        raise VariationInfeasibleError(msg, config_name=e.config_name) from e
    except Exception as e:
        msg = f"Variation failed. {variation_class.__name__}: {e}"
        logger.error(msg)
        progress_update_callback(msg)
        raise RuntimeError(msg) from e

    # Check if configs is None and return empty list
    if configs is None:
        msg = f"Variation failed. {variation_class.__name__}: No configs returned"
        logger.warning(msg)
        progress_update_callback(msg)
        raise RuntimeError(msg)

    # Collect transient (intermediate) files after variation has run
    campaign_transient_files = variation.get_campaign_transient_files()
    config_transient_files = []

    logger.debug(f"Variation {variation_class.__name__} completed successfully")
    return configs, input_files, campaign_transient_files, config_transient_files


def _backend_run_files(vast_dir, parameters):
    """Files the simulator backend declares its simulator needs, relative to the ``.vast``.

    Empty when no backend is declared, when the backend declares nothing, or when it
    cannot be resolved -- composition must not fail here on a backend problem that
    validation reports properly elsewhere.
    """
    from robovast.common.simulators import (  # pylint: disable=import-outside-toplevel
        ContainerQuery, backend_name, resolve_backend)

    execution = parameters.get("execution", {}) or {}
    name = backend_name(execution)
    if not name:
        return []
    try:
        backend = resolve_backend(name, vast_dir)
        cfg = _backend_cfg(backend, execution, name)
        declared = backend.input_files(cfg, execution)
        if isinstance(declared, ContainerQuery):
            return _run_input_files_query(declared, vast_dir)
        return [str(p) for p in (declared or [])]
    except Exception as exc:  # noqa: BLE001 - reported by validation, not here
        logger.debug("simulator backend declared no input files: %s", exc)
        return []


def _run_input_files_query(query, vast_dir):
    """Ask the simulator's own image which files a world is made of.

    A world is not one file -- it is the YAML, whatever it ``extends``, the MJCF that chain
    settles on, and the meshes that MJCF names -- and enumerating that needs the simulator,
    which a backend must not import. So the backend states the question and the answer comes
    from the image that will run the campaign.

    Until this existed, a world extending another *campaign* file staged only the YAML: the
    run then failed in the container on a parent that never travelled, after the image pull.

    Paths outside the campaign directory are dropped rather than staged. They are the ones
    that arrived with the image (a packaged world's meshes), and copying them would put a
    second, diverging copy of an installed asset into the campaign.
    """
    runner = _make_container_runner(query.spec)
    if runner is None:
        return []
    lines = []
    try:
        runner.run(query.command, lines.append)
    finally:
        runner.close()

    payload = _last_json_line(lines)
    if payload is None:
        raise RuntimeError(
            "the simulator backend's input-files query printed no JSON; its output was: "
            + " | ".join(str(line) for line in lines[-3:]))
    if payload.get("packaged"):
        return []

    root = os.path.abspath(vast_dir)
    relative = []
    for path in payload.get("inputs") or []:
        absolute = os.path.abspath(str(path))
        if absolute.startswith(root + os.sep):
            relative.append(os.path.relpath(absolute, root))
    return relative


def _last_json_line(lines):
    """The JSON object a container command printed, or ``None``.

    Scanned from the end because a runner interleaves its own progress lines with the
    command's stdout; the answer is the last thing that parses as a JSON object.
    """
    for line in reversed(lines):
        text = str(line).strip()
        if not text.startswith("{"):
            continue
        try:
            payload = json.loads(text)
        except ValueError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _check_declared_outputs(config, classes_and_parameters, scenario_parameters,
                            parameters, vast_dir):
    """Check what each variation says it will write, before any of them runs.

    A variation's outputs were never checked at all: the scenario-file check covers only the
    hand-written ``parameters:`` block, so a plugin writing a parameter the ``.osc`` does not
    declare was discovered by the *run* -- after the image pull and the pod schedule. Any
    plugin implementing :meth:`~robovast.common.variation.base_variation.Variation.declared_outputs`
    is checked here instead, on both channels.

    A plugin that declares nothing is skipped rather than guessed at: ``{}`` means
    "undeclared", which is every third-party plugin and was the state of every plugin before
    this existed.

    The ``sim`` half checks the destination is addressable in the backend's schema. Whether
    the *path inside* an override actually exists in a particular world is a question only
    the simulator can answer, so a typo there is still refused in the container.
    """
    valid_names = [p.get('name') for p in (scenario_parameters or [])
                   if isinstance(p, dict) and 'name' in p]
    execution = parameters.get('execution', {}) or {}

    backend = backend_key_checker = None
    for variation_class, variation_parameters in classes_and_parameters:
        try:
            declared = variation_class.declared_outputs(variation_parameters) or {}
        except Exception as exc:  # noqa: BLE001 - a plugin that cannot answer is not checked
            logger.debug("%s did not declare its outputs: %s",
                         variation_class.__name__, exc)
            continue

        unknown = [n for n in declared.get('scenario', []) if n not in valid_names]
        if unknown and valid_names:
            raise ValueError(
                f"Scenario '{config['name']}': {variation_class.__name__} writes "
                f"{unknown}, which the scenario file does not declare. "
                f"Valid parameters are: {valid_names}")

        sim_outputs = declared.get('sim', [])
        if not sim_outputs:
            continue
        if backend_key_checker is None:
            from robovast.common.simulators import (  # pylint: disable=import-outside-toplevel
                backend_name, resolve_backend, resolve_sim_path)
            name = backend_name(execution)
            if not name:
                raise ValueError(
                    f"Scenario '{config['name']}': {variation_class.__name__} writes to the "
                    f"'sim' channel ({sim_outputs}), but this campaign declares no simulator "
                    "backend under execution.containers.simulation")
            backend = resolve_backend(name, vast_dir)
            backend_key_checker = (name, resolve_sim_path)
        name, resolve = backend_key_checker
        for path in sim_outputs:
            resolve(backend, path, name)


#: OSC types whose values carry an ``entity_name``. The scenario file declares them
#: (``static_objects: list of spawn_entity``), so which parameters name entities is a fact
#: RoboVAST reads rather than a convention it invents -- the same source that says whether a
#: goal parameter takes one pose or a list.
_ENTITY_TYPES = ("spawn_entity",)


def entity_bearing_parameters(scenario_parameters) -> list:
    """Names of the scenario parameters whose values carry entity names."""
    return [p.get("name") for p in (scenario_parameters or [])
            if isinstance(p, dict)
            and str(p.get("type", "")).replace("listof", "") in _ENTITY_TYPES]


def _entity_names_in(value) -> set:
    """Every ``entity_name`` in a scenario-parameter value, at any depth."""
    if isinstance(value, dict):
        found = {str(value["entity_name"])} if value.get("entity_name") else set()
        for item in value.values():
            found |= _entity_names_in(item)
        return found
    if isinstance(value, list):
        return set().union(*(_entity_names_in(v) for v in value)) if value else set()
    return set()


class WorldQueryUnavailable(RuntimeError):
    """The world could not be described, with the reason a caller can act on.

    Not "this campaign is wrong": it is unverifiable from here. Kept distinct from a plain
    ``ValueError`` so a caller pre-*checking* can carry on (and warn) while a caller *asking*
    can report why -- collapsing the two is what made a failed lookup look like a clean check.
    """


def describe_world_payload(execution, block, vast_dir, *, entities: bool = False,
                           targets: str = "") -> tuple[dict, str]:
    """Ask the simulator what a world provides. Returns ``(payload, image)``.

    One place where this query is run, because there are two callers with the same needs and
    the same traps: the override pre-check below, and the ``describe_world`` operation a caller
    uses to *write* an override in the first place. Raises
    :class:`WorldQueryUnavailable` when no answer is possible, naming which of the reasons it
    is -- no backend, a backend that cannot describe, an image that has to be built first, no
    container runner here, or a simulator that answered nothing.

    A **partial** answer is not one of those reasons: a simulator that exits non-zero having still
    printed a payload answered what it could, and that payload is returned with whatever it says in
    its own ``errors``. Every consumer here already treats an absent half as unverifiable rather
    than as wrong, so half an answer is strictly better than none.
    """
    from robovast.common.execution import \
        is_build_image_ref  # pylint: disable=import-outside-toplevel
    from robovast.common.simulators import backend_name  # pylint: disable=import-outside-toplevel
    from robovast.common.simulators import resolve_backend

    name = backend_name(execution or {})
    if not name:
        raise WorldQueryUnavailable(
            "this campaign declares no simulator backend, so it has no world to describe")
    backend = resolve_backend(name, vast_dir)
    query = backend.describe_query(_validated_block(backend, block, name), execution,
                                   entities=entities, targets=targets)
    if query is None:
        raise WorldQueryUnavailable(f"the {name!r} backend cannot describe a world")
    image = getattr(query.spec, "image", "") or ""
    if is_build_image_ref(image):
        raise WorldQueryUnavailable(
            f"this campaign's world is described by its own built image ({image}), which does "
            "not exist yet -- build the experiment image first")
    runner = _make_container_runner(query.spec)
    if runner is None:
        raise WorldQueryUnavailable("no container runner is available here")
    lines = []
    try:
        expose = getattr(runner, "expose", None)
        if expose is not None:
            # Mirrors what a real run mounts: the campaign's own files, at the same
            # CONFIG_MOUNT path _config_in_container() already assumes when it rewrites
            # the command. Without this the query fails even once the path is right.
            from robovast.common.simulators import \
                CONFIG_MOUNT  # pylint: disable=import-outside-toplevel
            expose(vast_dir, CONFIG_MOUNT)
        runner.run(query.command, lines.append)
    except Exception as exc:  # noqa: BLE001 - a failed container is a reason, not a traceback
        # A non-zero exit that nonetheless PRINTED a payload is a partial answer, not a failure: a
        # simulator that could not build the world can still say which plugin keys it has, and that
        # half needs no build. Taking it costs nothing and is what keeps the pre-check alive for a
        # world whose model does not compile here -- the reason travels in the payload's own
        # ``errors``. Generic on purpose: nothing here knows which half was lost.
        partial = _last_json_line(lines)
        if partial is not None:
            return partial, image
        # The command's own last words, not the runner's: an old image whose simulator does not
        # know a flag says so itself ("unrecognized arguments: --overridable"), and that names
        # the remedy. Without this the CalledProcessError left the service returning a bare 500.
        raise WorldQueryUnavailable(
            f"{name} could not describe this world in {image}: "
            f"{_command_failure(lines) or str(exc)}") from None
    finally:
        runner.close()
    payload = _last_json_line(lines)
    if payload is None:
        raise WorldQueryUnavailable(
            f"{name} could not describe this world in {image}: "
            f"{_command_failure(lines) or '(no output)'}")
    return payload, image


def _command_failure(lines) -> str:
    """The most explanatory line a failed container printed, or ``""``.

    Scanned from the end for the command's own diagnostic, skipping the runner's framing (the
    ``docker run`` echo, an ``Output:`` header) -- what a caller needs is the simulator's
    complaint, not how it was invoked.
    """
    for line in reversed([str(item).strip() for item in lines]):
        if not line or line.startswith(("docker ", "Command failed", "Output:", "usage:")):
            continue
        return line[:400]
    return ""


def _check_sim_against_world(execution, configs, vast_dir, scenario_parameters=None):
    """Check every ``sim`` override addresses a plugin the world actually has.

    The ``sim`` channel is writable without this but not *discoverable*: a campaign writes
    ``plugins.floorplna.size``, composes cleanly, ships, pulls the image, schedules the pod,
    and only then is refused by ``apply_overrides``. Nothing before the container could tell,
    because resolving a world's ``extends`` chain needs the simulator.

    Checked per **distinct** block, and only when the campaign actually overrides something --
    a container run is not free, and a campaign that only selects worlds has nothing to check.

    Only the *plugin key* is verified. A path a world leaves at its default is legitimately
    absent from what the simulator reports, so flagging it would refuse a correct campaign;
    a plugin key matching nothing is unambiguous.

    A backend that cannot describe a world, or a runner that is not available here, means no
    check -- the campaign behaves exactly as it did before, and is refused in the container if
    it is wrong. A **partial** description means no check for the half it could not answer: the
    entity check needs a compiled model, the plugin-key check does not, so a world that fails to
    build still gets the cheaper one. Each half that goes unchecked says so.
    """
    from robovast.common.simulators import backend_name  # pylint: disable=import-outside-toplevel
    from robovast.common.simulators import resolve_backend, sim_override_keys

    name = backend_name(execution or {})
    if not name:
        return
    backend = resolve_backend(name, vast_dir)
    entity_params = entity_bearing_parameters(scenario_parameters)

    # Group by resolved block: what a world offers depends on the world, not on the
    # configuration, so one description serves every configuration sharing it.
    by_block = {}
    for config in configs:
        block = config.get("sim") or {}
        by_block.setdefault(json.dumps(block, sort_keys=True, default=str),
                            (block, []))[1].append(config)

    for block, sharing in by_block.values():
        wanted = sim_override_keys(backend, block)
        named = set()
        for config in sharing:
            params = config.get("config") or {}
            for param in entity_params:
                named |= _entity_names_in(params.get(param))
        if not wanted and not named:
            continue

        try:
            payload, _image = describe_world_payload(
                execution, block, vast_dir, entities=bool(named))
        except WorldQueryUnavailable as exc:
            # Say so. At debug level this silently disarmed the check for exactly the campaigns
            # most in need of it -- one whose world ships in its own built image answers
            # nothing, and a misspelled plugin key then sailed through to the container.
            logger.warning(
                "sim overrides were not pre-checked (%s): %s. They are still refused in the "
                "container if they are wrong.",
                exc, ", ".join(sorted(wanted)) or "the entities this scenario names")
            return

        errors = payload.get("errors") or {}
        if errors and named:
            # The plugin-key half below still runs -- what a partial answer costs is the entity
            # check, and which half went is worth a line. Silence here would read as a clean check.
            logger.warning(
                "the entities this scenario names were not pre-checked (%s): %s. They are still "
                "refused in the container if they are wrong.",
                "; ".join(f"{k}: {v}" for k, v in sorted(errors.items())), ", ".join(sorted(named)))

        available = {str(p.get("key")) for p in (payload.get("plugins") or [])}
        unknown = sorted(k for k in wanted if k not in available)
        if unknown:
            raise ValueError(
                f"sim override targets no plugin in this world: {', '.join(unknown)}. "
                f"The world has: {', '.join(sorted(available)) or '(none)'}")

        compiled = payload.get("entities")
        if named and compiled is not None:
            missing = sorted(named - set(compiled))
            if missing:
                raise ValueError(
                    "the scenario drives entities this world does not compile: "
                    f"{', '.join(missing)}. The world has: "
                    f"{', '.join(sorted(compiled)) or '(none)'}. Nothing can create them at "
                    "run time -- which entities exist is settled when the model compiles.")


def _validated_block(backend, block, name):
    """The backend's validated view of a resolved ``sim`` block."""
    from robovast.common.simulators import _validated_cfg  # pylint: disable=import-outside-toplevel
    return _validated_cfg(backend, dict(block or {}), name)


def _resolve_config_sim_blocks(configs, parameters, vast_dir, run_files,
                               scenario_parameters=None):
    """Resolve every configuration's ``sim`` block, and stage the worlds they name.

    Runs **after** the variation loop, because that is the first point at which a
    configuration's simulator settings exist: the campaign's ``simulation`` block is only
    the default, and a variation writing ``sim_values`` overlays it per cell.

    Two things come out of it. Each configuration carries its resolved block (recorded in
    ``configurations.yaml``, written to ``sim.config``, and read by both lanes at dispatch),
    and the **union** of the worlds those blocks name joins ``run_files`` -- once per
    distinct block, since a campaign varying its world has several and each has to be
    mounted for the simulator to open it.

    Errors are raised when the campaign actually uses the channel and swallowed when it does
    not: a ``sim:`` path that no backend accepts is a mistake worth failing composition for,
    while a backend that merely cannot be imported here must not break a campaign that never
    mentions it (validation reports that properly elsewhere).
    """
    from robovast.common.simulators import backend_name  # pylint: disable=import-outside-toplevel
    from robovast.common.simulators import flatten_sim_block, merge_sim_block, sim_input_files

    execution = parameters.get("execution", {}) or {}
    if not backend_name(execution):
        return

    authored = {c.get("name"): (c.get("sim") or {})
                for c in (parameters.get("configuration") or [])}
    uses_channel = any(authored.values()) or any(c.get("sim") for c in configs)

    seen_blocks = []
    for config in configs:
        # The authored per-configuration block first, then what variations wrote, so a
        # variation's value wins over the fixed one it varies -- the same precedence
        # `parameters:` and a scenario-parameter variation already have.
        sim_values = flatten_sim_block(authored.get(config.get("_config_name")) or {})
        sim_values.update(config.get("sim") or {})
        deploy_paths = {rel for rel, _ in (config.get("_config_files") or [])}
        try:
            resolved = merge_sim_block(
                execution, sim_values, vast_dir,
                deploy_paths=deploy_paths, config_name=config.get("name", ""))
        except Exception as exc:  # noqa: BLE001 - re-raised only where it is the user's
            if uses_channel:
                raise
            logger.debug("simulator backend contributed no sim block: %s", exc)
            return
        config["sim"] = resolved
        if resolved not in seen_blocks:
            seen_blocks.append(resolved)

    for block in seen_blocks:
        try:
            declared = sim_input_files(
                execution, block, vast_dir,
                run_query=lambda query: _run_input_files_query(query, vast_dir))
        except Exception as exc:  # noqa: BLE001 - as above
            if uses_channel:
                raise
            logger.debug("simulator backend declared no input files: %s", exc)
            continue
        for rel in declared:
            if rel not in run_files:
                run_files.append(rel)

    try:
        _check_sim_against_world(execution, configs, vast_dir, scenario_parameters)
    except ValueError:
        # The campaign's own mistake, and the whole point of checking here.
        raise
    except Exception as exc:  # noqa: BLE001 - an unavailable checker is not a bad campaign
        logger.debug("sim overrides were not pre-checked: %s", exc)


def _backend_cfg(backend, execution, name):
    """The backend's own validated config block."""
    from robovast.common.config import \
        SIMULATION_CONTAINER  # pylint: disable=import-outside-toplevel
    from robovast.common.simulators import _validated_cfg  # pylint: disable=import-outside-toplevel
    block = (execution.get("containers") or {}).get(SIMULATION_CONTAINER) or {}
    return _validated_cfg(backend, dict(block), name)


def _generated_run_files(vast_dir, parameters, records):
    """Files produced by ``execution.generate``, as paths relative to the ``.vast``.

    Derived from each entry's declared ``out`` rather than from *records*, so the
    isolated compose subprocess -- which does not re-run the generators -- still picks up
    what the parent produced without importing a single generator class.
    """
    entries = parameters.get("execution", {}).get("generate") or []
    if not entries:
        return []
    if records:
        return [rel for record in records for rel in record.get("outputs", [])]
    found = []
    for index, entry in enumerate(entries):
        name, params = parse_generate_entry(entry, index)
        out_dir = resolve_out_dir(params.get("out"), vast_dir,
                                  f"execution.generate[{index}].{name}")
        if not os.path.isdir(out_dir):
            raise missing_input_error(
                [(f"execution.generate[{index}].{name}.out", params.get("out"), out_dir)])
        found.extend(collect_output_files(out_dir, vast_dir))
    return found


def collect_filtered_files(filter_pattern, rel_path):
    """Collect files from scenario directory that match the filter patterns"""
    filtered_files = []
    logger.debug(f"Collecting filtered files from: {rel_path}")
    if not filter_pattern:
        return filtered_files
    for root, _, files in os.walk(rel_path):
        for file in files:
            file_path = os.path.join(root, file)
            if matches_patterns(file_path, filter_pattern, rel_path):
                key = os.path.relpath(file_path, rel_path)
                filtered_files.append(key)

    return filtered_files


def matches_patterns(file_path, patterns, base_dir):
    """Check if a file matches any of the gitignore-like patterns with support for ** recursive matching"""
    if not patterns:
        return False

    # Get relative path from base directory
    try:
        rel_path = os.path.relpath(file_path, base_dir)
    except ValueError:
        # Path is not relative to base_dir
        return False

    # Normalize path separators for consistent matching
    rel_path = rel_path.replace(os.sep, '/')

    for pattern in patterns:
        if _match_pattern(rel_path, pattern):
            return True

    return False


def _match_pattern(rel_path, pattern):
    """Match a single pattern against a relative path, supporting ** for recursive matching"""
    # Normalize pattern separators
    pattern = pattern.replace(os.sep, '/')

    # Handle directory patterns (ending with /)
    if pattern.endswith('/'):
        pattern = pattern[:-1]
        # Check if any parent directory matches
        parts = rel_path.split('/')
        for i in range(len(parts)):
            parent_path = '/'.join(parts[:i+1])
            if _glob_match(parent_path, pattern):
                return True
        return _glob_match(os.path.dirname(rel_path), pattern)
    else:
        # Handle file patterns
        if _glob_match(rel_path, pattern):
            return True
        # Also check just the filename
        if _glob_match(os.path.basename(rel_path), pattern):
            return True
        return False


def _glob_match(path, pattern):
    """Enhanced glob matching with support for ** recursive patterns"""
    # Handle ** patterns
    if '**' in pattern:
        return _match_recursive_pattern(path, pattern)
    else:
        # Use standard fnmatch for simple patterns
        return fnmatch.fnmatch(path, pattern)


def _match_recursive_pattern(path, pattern):
    """Match patterns containing ** for recursive directory matching"""

    # Split pattern by ** to handle each part
    pattern_parts = pattern.split('**')

    if len(pattern_parts) == 1:
        # No ** in pattern, use standard matching
        return fnmatch.fnmatch(path, pattern)

    # Convert glob pattern to regex, handling ** specially
    regex_pattern = ''
    for i, part in enumerate(pattern_parts):
        if i > 0:
            # Add regex for ** (match zero or more path segments)
            regex_pattern += '(?:[^/]+/)*'

        # Convert glob to regex for this part
        if part:
            # Remove leading/trailing slashes to avoid double slashes
            part = part.strip('/')
            if part:
                # Convert fnmatch pattern to regex
                part_regex = fnmatch.translate(part).replace('\\Z', '')
                # Remove the (?ms: prefix and ) suffix that fnmatch.translate adds
                if part_regex.startswith('(?ms:'):
                    part_regex = part_regex[5:-1]
                regex_pattern += part_regex
                if i < len(pattern_parts) - 1:
                    regex_pattern += '/'

    # Ensure the pattern matches the entire string
    regex_pattern = '^' + regex_pattern + '$'

    try:
        return bool(re.match(regex_pattern, path))
    except re.error:
        # Fallback to simple fnmatch if regex fails
        return fnmatch.fnmatch(path, pattern)


def _get_variation_classes(scenario_config, vast_dir=""):
    """
    Read variation class names scenario

    A variation is named either by an installed ``robovast.variation_types``
    entry point or by a local ``<path>.py:<Class>`` file reference resolved
    relative to ``vast_dir`` (parity with search strategies/extractors and
    results postprocessing).
    """

    # Get the variation list from settings
    variation_list = scenario_config.get('variations', [])

    if not variation_list or not isinstance(variation_list, list):
        return []

    # Dynamically discover available variation classes from entry points
    available_classes = {}

    # Load variation types from robovast.variation_types entry point
    try:
        eps = entry_points()
        variation_eps = eps.select(group='robovast.variation_types')

        ep_list = list(variation_eps)
        if not ep_list:
            logger.warning("No variation types found in entry points. This usually means the package is not properly installed.")
            logger.warning("Try running: poetry install")
            print("WARNING: No variation type plugins found! Run 'poetry install' to register plugins.")

        for ep in ep_list:
            try:
                variation_class = ep.load()
                available_classes[ep.name] = variation_class
                logger.debug(f"Loaded variation type: {ep.name}")
            except Exception as e:
                logger.warning(f"Failed to load variation type '{ep.name}': {e}")
                print(f"Warning: Failed to load variation type '{ep.name}': {e}")
    except Exception as e:
        logger.error(f"Failed to load variation types from entry points: {e}")
        print(f"Warning: Failed to load variation types from entry points: {e}")

    # Extract variation class names from the list
    variation_classes = []
    for item in variation_list:
        if isinstance(item, dict):
            # Each item in the list should be a dict with one key (the class name)
            for class_name in item.keys():
                if class_name in available_classes:
                    variation_classes.append((available_classes[class_name], item[class_name]))
                elif is_file_ref(class_name):
                    # Local '<path>.py:<Class>' reference relative to the .vast dir.
                    variation_class = load_ref(class_name, 'robovast.variation_types', vast_dir)
                    errors = _validate_variation_class(class_name, variation_class)
                    if errors:
                        raise ValueError(
                            f"Invalid variation plugin '{class_name}': {'; '.join(errors)}")
                    variation_classes.append((variation_class, item[class_name]))
                else:
                    error_msg = f"Unknown variation class '{class_name}' found in variation file.\n"
                    if not available_classes:
                        error_msg += "No variation plugins are registered. This usually means the robovast package is not properly installed.\n"
                        error_msg += "To fix this, run: poetry install\n"
                        error_msg += "If you're in a CI environment, ensure 'poetry install' (without --no-root) has been executed."
                    else:
                        error_msg += f"Available variation types: {', '.join(available_classes.keys())}.\n"
                        error_msg += (
                            f"'{class_name}' is not built into robovast/robovast-nav. If it comes from a "
                            "third-party package (e.g. 'scenario_mt'), declare that package in the "
                            "top-level 'plugins:' list of your .vast so it is installed before composing, "
                            "e.g.:\n"
                            "  plugins:\n"
                            "  - 'my_plugin @ git+https://github.com/org/repo@ref'\n"
                            "Then re-run so the variation names resolve via its entry points.\n"
                            "Alternatively, use a '<path>.py:<Class>' file reference for a local module.")
                    raise ValueError(error_msg)

    return variation_classes


def _validate_relative_path(path, description="path"):
    """Validate that a path is relative and does not escape its base directory."""
    if os.path.isabs(path):
        raise ValueError(f"{description} must be relative, got absolute path: {path}")
    normalized = os.path.normpath(path)
    if normalized.startswith('..'):
        raise ValueError(f"{description} must not escape the base directory: {path}")


def _section(obj, key):
    """``obj[key]`` whether *obj* is a plain dict or a pydantic model, else ``None``.

    The config reaches this module both ways -- raw YAML during composition, validated models
    from the service -- and the nested ``visualization`` tree would otherwise need this pair of
    branches at every level.
    """
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None) if obj is not None else None


def _collect_analysis_input_files(parameters, base_dir=None):
    """Collect file paths referenced in the explorer notebooks and results.postprocessing."""
    analysis_files = []

    # Explorer notebooks: visualization.results.explorer.notebooks
    visualizations = _section(
        _section(_section(_section(parameters, 'visualization'), 'results'), 'explorer'),
        'notebooks') or []

    # Collect postprocessing files from results_processing section.
    # The top-level config key is ``results_processing`` (not ``results``).
    data = parameters.get('results_processing') or parameters.get('results')
    if isinstance(data, dict):
        postprocessing = data.get('postprocessing') or []
    elif data is not None and hasattr(data, 'postprocessing'):
        postprocessing = data.postprocessing or []
    else:
        postprocessing = []

    # The search section also carries postprocessing entries and an extract
    # plugin, all of which may be local file refs that must be bundled.
    search = parameters.get('search')
    search_postprocessing = []
    search_extract_plugin = None
    if isinstance(search, dict):
        search_postprocessing = search.get('postprocessing') or []
        extract = search.get('extract')
        if isinstance(extract, dict):
            search_extract_plugin = extract.get('plugin')

    for viz_entry in visualizations:
        if isinstance(viz_entry, dict):
            for _plugin_name, plugin_config in viz_entry.items():
                if isinstance(plugin_config, dict):
                    for _key, path in plugin_config.items():
                        if isinstance(path, str) and (path.endswith('.ipynb') or path.endswith('.py')):
                            analysis_files.append(path)

    def _collect_ref(value):
        """Collect a local module path from an entry-point/file ref or file value."""
        if not isinstance(value, str) or os.path.isabs(value):
            return
        path_part = value.rsplit(".py:", 1)[0] + ".py" if is_file_ref(value) else value
        candidate = os.path.join(base_dir, path_part) if base_dir else path_part
        if os.path.isfile(candidate):
            analysis_files.append(path_part)

    # A postprocessing plugin is referenced by its command *name* (an entry-point
    # name or a ``./path.py:Class`` file ref, e.g. ``./search/metrics.py:QuadMetrics``)
    # and may carry file-ref params. Collect local modules from both, across the
    # results_processing and search postprocessing lists.
    for pp_entry in [*postprocessing, *search_postprocessing]:
        if isinstance(pp_entry, str):
            _collect_ref(pp_entry)
        elif isinstance(pp_entry, dict):
            for plugin_name, plugin_params in pp_entry.items():
                _collect_ref(plugin_name)
                if isinstance(plugin_params, dict):
                    for _key, value in plugin_params.items():
                        _collect_ref(value)

    # The search extract plugin itself may be a local file ref.
    _collect_ref(search_extract_plugin)

    # Custom run-view panels ship a built Module-Federation bundle next to the .vast
    # (``visualization.results.run_view.panels: - custom: {remote: <dir-or-remoteEntry.js>}``). Stage every
    # file in each bundle dir so remoteEntry.js + its chunks land in _config/ and are served
    # per campaign at runtime. Package panels (entry-point types) ship their own assets and
    # are not collected here.
    visualization = parameters.get('visualization')
    panels = visualization.get('panels') if isinstance(visualization, dict) else None
    if base_dir and panels:
        for entry in panels:
            if isinstance(entry, dict) and 'type' not in entry and len(entry) == 1:
                (ptype, props), = entry.items()
            elif isinstance(entry, dict):
                ptype, props = entry.get('type'), entry
            else:
                continue
            if ptype != 'custom' or not isinstance(props, dict):
                continue
            remote = props.get('remote')
            if not isinstance(remote, str) or os.path.isabs(remote):
                continue
            bundle_dir = os.path.dirname(remote) if remote.endswith('.js') else remote
            abs_bundle = os.path.join(base_dir, bundle_dir) if bundle_dir else base_dir
            if not os.path.isdir(abs_bundle):
                continue
            for root, _dirs, files in os.walk(abs_bundle):
                for fn in files:
                    rel = os.path.relpath(os.path.join(root, fn), base_dir)
                    if rel not in analysis_files:
                        analysis_files.append(rel)

    # Collect files declared by class-based postprocessing plugins via
    # get_files_to_copy().  This is how e.g. the ``command`` plugin ensures
    # that the referenced script ends up in _config/ so it is available at
    # execution time.
    if base_dir and postprocessing:
        # Lazy import to avoid circular dependency at module load time.
        from robovast.results_processing.postprocessing import \
            load_postprocessing_plugins  # pylint: disable=import-outside-toplevel
        plugins = load_postprocessing_plugins()
        for pp_entry in postprocessing:
            if isinstance(pp_entry, str):
                plugin_name, params = pp_entry, {}
            elif isinstance(pp_entry, dict) and len(pp_entry) == 1:
                plugin_name, params = next(iter(pp_entry.items()))
                if not isinstance(params, dict):
                    params = {}
            else:
                continue
            plugin_obj = plugins.get(plugin_name)
            if plugin_obj is not None and hasattr(plugin_obj, 'get_files_to_copy'):
                for f in plugin_obj.get_files_to_copy(base_dir, params):
                    if f not in analysis_files:
                        analysis_files.append(f)

    return analysis_files


# Bump this whenever the cache storage format changes, to auto-invalidate stale entries.
# 7: _run_files now also carries files produced by execution.generate, so a cached entry
# from 6 describes a different input set than the same .vast composes today.
_CACHE_FORMAT_VERSION = 7


def _build_generate_cache_key(
    variation_file: str,
    vast_dir: str,
    scenario_file: str,
    run_files: list,
    analysis_files: list,
    configurations: list,
    tolerate_infeasible: bool = False,
    image_project: str | None = None,
    image_project_tag: str | None = None,
) -> CacheKey:
    """Build a FileCache2 CacheKey covering every input that affects generate_scenario_variations.

    All ``add_file`` calls pass *base_dir=vast_dir* so that the key uses
    ``relpath(file, vast_dir)`` rather than basename, preventing collisions
    between different files that share the same name.
    """
    key = CacheKey()

    # Cache format version — bumped whenever the stored structure changes.
    key.add("cache_format_version", _CACHE_FORMAT_VERSION)

    # A cache entry composed with one tolerance policy must never satisfy a request
    # made with the other -- same file, different composition outcome.
    key.add("tolerate_infeasible", tolerate_infeasible)

    # Same reason: the composed data carries the *resolved* family image refs, so an entry
    # composed against one project must not satisfy a request for another. Without this a
    # dev run against a second registry would silently reuse the first campaign's images —
    # the one failure mode a per-campaign override must not have.
    key.add("image_project", image_project or "")
    key.add("image_project_tag", image_project_tag or "")

    # .vast file itself
    key.add_file(variation_file, base_dir=vast_dir)

    # scenario .osc file
    if scenario_file and os.path.exists(scenario_file):
        key.add_file(scenario_file, base_dir=vast_dir)

    # files matched by execution.run_files globs
    for rel in run_files:
        abs_path = os.path.join(vast_dir, rel)
        if os.path.exists(abs_path):
            key.add_file(abs_path, base_dir=vast_dir)

    # analysis notebooks / scripts referenced in evaluation/results_processing
    for rel in analysis_files:
        abs_path = os.path.join(vast_dir, rel)
        if os.path.exists(abs_path):
            key.add_file(abs_path, base_dir=vast_dir)

    # files linked in each configuration block (map files, nav configs, etc.)
    for config_block in configurations:
        for rel in sorted(collect_paths_from_config(config_block, vast_dir)):
            abs_path = os.path.join(vast_dir, rel)
            if os.path.exists(abs_path):
                key.add_file(abs_path, base_dir=vast_dir)

    # Hash the source code of every variation plugin referenced in the .vast file.
    # This ensures a cache miss when plugin implementation changes, even if the
    # .vast file and input data files are untouched.
    all_variation_names = tuple(sorted({
        class_name
        for config_block in configurations
        for item in config_block.get('variations', [])
        if isinstance(item, dict)
        for class_name in item.keys()
    }))
    key.add("variation_entrypoints_hash", hash_variation_entrypoints(all_variation_names))

    return key


def _result_to_transport(result: dict) -> dict:
    """Serialize a composition ``result`` to a JSON-safe transport dict.

    This is the single serialization used by BOTH the on-disk cache and the
    isolated-compose IPC boundary, so a campaign composed in a subprocess is
    byte-for-byte identical to a cached one. Per-config ``_config_files`` /
    ``_config_transient_files`` ``(rel, path)`` tuples become tagged dicts (source
    files keep their absolute path; artifacts store the path relative to
    ``output_dir``); ephemeral fields (``_output_dir``, ``_transient_files``,
    ``_config_block``) are dropped.
    """
    transport = copy.deepcopy(result)
    transport["_transient_files"] = []
    transport.pop("_output_dir", None)
    for cfg in transport.get("configs", []):
        cfg.pop("_config_block", None)
        storable = []
        for rel, path in cfg.get("_config_files", []):
            if os.path.isabs(rel):
                raise ValueError(
                    f"_config_files entry has an absolute deploy path '{rel}' "
                    f"in config '{cfg.get('name')}'. "
                    "Variation plugins must use relative paths in _config_files.")
            if os.path.isabs(path):  # source file — stable project path
                storable.append({"rel": rel, "abs": path, "kind": "source"})
            else:  # artifact — relative to output_dir
                storable.append({"rel": rel, "rel_from_output": path, "kind": "artifact"})
        cfg["_config_files"] = storable
        cfg["_config_transient_files"] = [
            {"rel": rel, "rel_from_output": path}
            for rel, path in cfg.get("_config_transient_files", [])
        ]
    return convert_dataclasses_to_dict(transport)


def _result_from_transport(data: dict, output_dir) -> dict:
    """Reconstruct a composition result (in place) from a transport dict.

    Inverse of :func:`_result_to_transport`: tagged ``_config_files`` /
    ``_config_transient_files`` dicts become ``(rel, path)`` tuples again, and
    ``_output_dir`` is restored from the caller's *output_dir* so relative artifact
    paths resolve. Plugin-free — safe to run in the parent process.
    """
    for cfg in data.get("configs", []):
        rebuilt = []
        for entry in cfg.get("_config_files", []):
            rel = entry["rel"]
            if entry["kind"] == "source":
                rebuilt.append((rel, entry["abs"]))
            else:  # artifact — keep relative to output_dir
                rebuilt.append((rel, entry["rel_from_output"]))
        cfg["_config_files"] = rebuilt
        cfg["_config_transient_files"] = [
            (entry["rel"], entry["rel_from_output"])
            for entry in cfg.get("_config_transient_files", [])
        ]
    if output_dir is not None:
        data["_output_dir"] = os.path.abspath(output_dir)
    return data


def _compose_isolated(variation_file, output_dir, use_cache, progress_update_callback,
                      tolerate_infeasible=False, image_project=None,
                      image_project_tag=None):
    """Compose a ``plugins:``-declaring .vast in an isolated subprocess.

    The worker leads ``sys.path`` with the project's ``.robovast_plugins`` so the
    plugin's pinned dependencies (which may conflict with the service's — e.g. a
    forked rdflib) win in that fresh process and never touch the long-lived robovast
    process. Only ``campaign_data`` crosses back, as the cache-transport JSON;
    artifacts are written into the shared *output_dir* on disk. The worker's output
    (pip install progress, composition progress, and any plugin traceback) is
    streamed live and, on failure, surfaced in the raised error.
    """
    if output_dir is None:
        output_dir = tempfile.mkdtemp(prefix="robovast_isolated_compose_")
    output_dir = os.path.abspath(output_dir)
    os.makedirs(output_dir, exist_ok=True)

    env = dict(os.environ)
    env[_ISOLATED_ENV] = "1"
    # A backend aux-container factory (a ContextVar) cannot cross into the worker;
    # signal it to fail clearly if a variation needs one (see _make_container_runner).
    if _container_runner_factory.get() is not None:
        env[_ISOLATED_NO_BACKEND] = "1"

    with tempfile.TemporaryDirectory(prefix="robovast_compose_job_") as jobdir:
        result_path = os.path.join(jobdir, "result.json")
        job_path = os.path.join(jobdir, "job.json")
        with open(job_path, "w", encoding="utf-8") as f:
            json.dump({
                "variation_file": os.path.abspath(variation_file),
                "output_dir": output_dir,
                "use_cache": bool(use_cache),
                "tolerate_infeasible": bool(tolerate_infeasible),
                # In the job file, not the env: the worker composes for exactly this
                # campaign, and the parent process may be composing others against other
                # projects at the same time. An inherited env var would be whichever
                # campaign set it last.
                "image_project": image_project,
                "image_project_tag": image_project_tag,
                "result_path": result_path,
            }, f)

        cmd = [sys.executable, "-m", "robovast.common.compose_worker", job_path]
        output_lines: list = []
        # Merge stderr into stdout so a full pipe on either stream cannot deadlock;
        # the plugin traceback (stderr) is interleaved and captured for the error.
        proc = subprocess.Popen(  # nosec B603 - fixed module, config-derived job file
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, env=env)
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            output_lines.append(line)
            print(line, flush=True)
            progress_update_callback(line)
        returncode = proc.wait()

        if returncode != 0:
            tail = "\n".join(output_lines[-25:])
            raise RuntimeError(
                f"Isolated plugin composition failed (exit {returncode}):\n{tail}")

        try:
            with open(result_path, encoding="utf-8") as f:
                transport = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            tail = "\n".join(output_lines[-25:])
            raise RuntimeError(
                f"Isolated plugin composition produced no result ({e}):\n{tail}") from e

    return _result_from_transport(transport, output_dir)


def generate_scenario_variations(variation_file, progress_update_callback=None, variation_classes=None, output_dir=None, use_cache=True, isolate_plugins=True, tolerate_infeasible=False, image_project=None, image_project_tag=None):
    """Generate all scenario variation configs from a .vast file.

    ``image_project`` / ``image_project_tag`` select which project the RoboVAST image
    family resolves from for *this* campaign (``None`` = the process environment's).
    They only affect ``family:`` refs; a container image the ``.vast`` states is
    untouched.

    ``tolerate_infeasible`` controls what happens when a variation raises
    :class:`~.variation.base_variation.VariationInfeasibleError` (a specific
    parameter draw cannot be realized, as opposed to a plugin bug): when
    ``False`` (the default — batch-mode campaigns and direct callers) it
    propagates and aborts composition, same as any other exception; when
    ``True`` (search-mode composition, via :class:`~robovast.search.compose.Compose`)
    the affected top-level config block is dropped and composition continues
    with the rest. Every other exception always propagates regardless of this
    flag.

    Caching is active for all flows when ``use_cache=True``.  Two cache
    entries are stored under ``<vast_dir>/.cache/``:

    * ``config_generation_{key}.json`` — config metadata.  Per-config
      ``_config_files`` entries are stored with kind ("source" / "artifact").
    * ``config_generation_artifacts_{key}.tar.gz`` — the entire output_dir
      archived as a tarball (created whenever output_dir has contents).

    On a cache hit the metadata JSON is returned immediately and, when an
    ``output_dir`` was provided, the artifact tarball is extracted there so
    that ``_config_files`` absolute paths are valid.

    When the .vast declares external variation-plugin packages (``plugins:``) and
    ``isolate_plugins`` is true (the default), the composition runs in an isolated
    subprocess (:func:`_compose_isolated`) so the plugin and its pinned dependencies
    are imported there — never in this long-lived process — and cannot clash with
    robovast's own. Only ``campaign_data`` crosses back, and it is returned exactly as
    the in-process path returns it: **a bare dict, on every path**. It used to be a
    ``(campaign_data, {})`` tuple here only, which no caller unpacked — so every ``.vast``
    declaring ``plugins:`` composed fine and then died on ``campaign_data["vast"] = ...``
    in :mod:`robovast.search.compose` with "'tuple' object does not support item
    assignment". Pass ``isolate_plugins=False`` to compose in-process when a caller needs
    live variation GUI classes. A warm cache hit returns without forking. Built-in-only
    vasts (no ``plugins:``) always compose in-process.
    """
    if not progress_update_callback:
        progress_update_callback = logger.debug
    progress_update_callback("Start generating configs.")

    _maybe_disable_ssl_verification()

    # Every path this function derives (`vast`, `scenario_file`, run/config files) hangs off this one,
    # and they outlive the call: they are cached under a key that is already abspath-normalized, and
    # written into the campaign's configurations.yaml. Left relative, a cache entry written by a
    # caller in one directory is replayed by a caller in another -- which is how a `vast` CLI run from
    # the repo root handed the service a project-relative `scenario.osc` to resolve again.
    variation_file = os.path.abspath(variation_file)

    parameters = load_config(variation_file)

    # Get scenario file from configuration section
    configurations = parameters.get('configuration', [])

    vast_dir = os.path.abspath(os.path.dirname(variation_file))

    # Derived campaign inputs, BEFORE run_files are collected (see
    # robovast.common.input_generation). Ordering is the whole point: a glob covering a
    # generator's output dir must not see a half-built tree, and the generated bytes must
    # reach hash_run_files -- so a campaign run against a changed world gets a different
    # config identifier instead of silently reusing the old one. Skipped in the isolated
    # compose subprocess: the parent already produced the files, and expanding them into
    # run_files needs no plugin import.
    generated_records = []
    if os.environ.get(_ISOLATED_ENV) != "1":
        generated_records = run_input_generators(
            vast_dir,
            parameters.get("execution", {}).get("generate") or [],
            progress_update_callback,
            container_runner_factory=_container_runner_factory.get(),
            use_cache=use_cache,
        )

    run_files = []
    # Get run_files patterns from config
    run_files_patterns = parameters.get("execution", {}).get("run_files", [])
    if run_files_patterns:
        additional_run_files = collect_filtered_files(run_files_patterns, os.path.dirname(variation_file))
        progress_update_callback(f"Loaded {len(run_files_patterns)} run_files patterns (found {len(additional_run_files)} files).")
        for pattern in run_files_patterns:
            if not collect_filtered_files([pattern], os.path.dirname(variation_file)):
                logger.warning(
                    "execution.run_files pattern '%s' did not match any files. "
                    "Check the pattern or whether the files exist.",
                    pattern,
                )
        run_files.extend(additional_run_files)

    # Generated outputs join run_files, so everything downstream -- the config-identity
    # hash, the copy into <campaign>/_config/, the /config/<path> bind mount into the run
    # -- treats them exactly like hand-written inputs, with no second code path. In the
    # isolated subprocess this re-derives them from `out` on disk without loading any
    # generator (generated_records is empty there).
    for rel in _generated_run_files(vast_dir, parameters, generated_records):
        if rel not in run_files:
            run_files.append(rel)

    # And what the simulator backend says has to travel -- typically a world declared as a
    # path rather than a package ref. Also run_files, for the same reason generated
    # outputs are: the file has to be MOUNTED at /config/<path> for the simulator to open
    # it, archived into <campaign>/_config/ for the run view to rebuild geometry from, and
    # hashed into the config identity because a changed world is a changed experiment.
    # `_input_files` does only the middle one, so it is the wrong list.
    #
    # Declared by the backend rather than written by the campaign: a `.vast` naming its
    # world under `config:` and then again under `run_files:` states one fact twice, and
    # the failure when the second is forgotten is remote from the cause -- the simulator
    # cannot open a path that was never mounted.
    #
    # This is the CAMPAIGN DEFAULT only. A world belongs to a *configuration*, so the ones
    # configurations actually resolve to are collected after the variation loop, by
    # `_resolve_config_sim_blocks`. The default is still staged here because it is what the
    # `.vast` declares and therefore what the composition cache key must cover; a campaign
    # whose every configuration replaces it simply carries one file it never opens.
    for rel in _backend_run_files(vast_dir, parameters):
        if rel not in run_files:
            run_files.append(rel)

    # Get scenario_file from execution section (resolved early for cache key)
    execution_scenario_file_name = parameters.get('execution', {}).get('scenario_file')

    # Validate scenario_file path
    if execution_scenario_file_name:
        _validate_relative_path(execution_scenario_file_name, "execution.scenario_file")

    scenario_file = os.path.join(os.path.dirname(variation_file), execution_scenario_file_name) if execution_scenario_file_name else None

    if scenario_file is None:
        raise ValueError("No scenario_file specified in execution section of the variation file. Please add 'scenario_file' to the execution section.")

    # Checked here, at the first point the path is known, rather than where it is first
    # *read*: the cache key skips a non-existent scenario file, so an unchecked typo
    # surfaced deep in campaign staging as a bare copy error naming only the path.
    if not os.path.isfile(scenario_file):
        raise missing_input_error([
            ("execution.scenario_file", execution_scenario_file_name, scenario_file)])

    # Collect analysis notebook files (resolved early for cache key)
    analysis_files = _collect_analysis_input_files(parameters, base_dir=os.path.dirname(variation_file))
    for af in analysis_files:
        _validate_relative_path(af, "analysis file")

    # Whether this composition must run in an isolated subprocess: the .vast declares
    # external variation-plugin packages (``plugins:``), the caller allows isolation
    # (the interactive GUI editor opts out to keep live GUI classes), variation
    # classes were not pre-supplied, and we are not already inside the isolated
    # worker. When true, the plugin is imported only in that subprocess — never in
    # this (long-lived) process — so its pinned dependencies cannot clash with ours.
    # The flag also gates the cache-hit GUI rebuild below, which would import the
    # plugin in-process.
    should_isolate = (
        bool(parameters.get("plugins"))
        and isolate_plugins
        and variation_classes is None
        and os.environ.get(_ISOLATED_ENV) != "1"
    )

    # --- Cache check ---
    # Cache is active for all flows when use_cache=True and variation_classes is None.
    # Two cache entries share the same key:
    #   config_generation_{key}.json      – config metadata
    #   config_generation_artifacts_{key}.tar.gz – artifact files written to output_dir
    #     by variation plugins (only created/restored when non-empty _config_files exist)
    _cache_enabled = use_cache and variation_classes is None
    if _cache_enabled:
        _cache_meta = FileCache2(vast_dir, "config_generation_", suffix=".json")
        _cache_artifacts = FileCache2(vast_dir, "config_generation_artifacts_", suffix=".tar.gz")
        _cache_key = _build_generate_cache_key(
            variation_file=os.path.abspath(variation_file),
            vast_dir=vast_dir,
            scenario_file=scenario_file,
            run_files=run_files,
            analysis_files=analysis_files,
            configurations=configurations,
            tolerate_infeasible=tolerate_infeasible,
            image_project=image_project,
            image_project_tag=image_project_tag,
        )
        _cached = _cache_meta.get_json(_cache_key)
        if _cached is not None:
            logger.debug("Cache HIT for generate_scenario_variations (%s)", variation_file)
            # Restore the whole output_dir from the tarball when the caller wants it.
            if output_dir is not None:
                _tar_path = _cache_artifacts.get(_cache_key, content=False)
                if _tar_path is not None:
                    os.makedirs(output_dir, exist_ok=True)
                    with tarfile.open(_tar_path, "r:gz") as tar:
                        tar.extractall(output_dir)  # nosec – trusted local cache
                    logger.debug("Restored output_dir from cache tar to %s", output_dir)
            # Reconstruct _config_files/_config_transient_files as (rel, path) tuples
            # and expose _output_dir — the same transform the isolated boundary uses,
            # so cached and freshly-composed results are structurally identical.
            _cached = _result_from_transport(_cached, output_dir)
            progress_update_callback("Loaded configurations from cache (no changes detected).")
            return _cached
        logger.debug("Cache MISS for generate_scenario_variations (%s)", variation_file)
    else:
        _cache_meta = None
        _cache_artifacts = None
        _cache_key = None

    # Cache miss for a plugin campaign: compose in an isolated subprocess so the
    # plugin (and its pinned deps) are imported there, never in this process. The
    # worker writes artifacts into the shared output_dir and returns campaign_data;
    # GUI classes are skipped (headless callers discard them). The worker itself
    # writes the cache, so the next build hits the fast path above without forking.
    if should_isolate:
        return _compose_isolated(variation_file, output_dir, use_cache, progress_update_callback,
                                 tolerate_infeasible, image_project=image_project,
                                 image_project_tag=image_project_tag)

    # About to compose (cache miss, or caching disabled). Ensure any variation-plugin
    # packages the .vast declares in ``plugins:`` are installed into the workspace's
    # ``.robovast_plugins/`` dir and on ``sys.path`` before resolving variation types
    # from entry points. Idempotent: in a controller pod the dir was staged with the
    # project so this only adjusts ``sys.path``. Skipped when the caller supplies
    # precomputed ``variation_classes``. In the isolated worker this is where the
    # plugin actually gets installed/imported (``_ISOLATED_ENV`` is set, so we reach
    # here rather than re-forking).
    if variation_classes is None:
        ensure_workspace_plugins(vast_dir, parameters.get("plugins"))

    configs = []
    campaign_input_files = []
    campaign_transient_files = []
    config_transient_files = []

    if output_dir is None:
        temp_path = tempfile.TemporaryDirectory(prefix="robovast_variation_")
        output_dir = temp_path.name

    general_parameters = parameters.get('general', {})

    # Get scenario parameters once (same for all configurations)
    scenario_param_dict = get_scenario_parameters(scenario_file)
    existing_scenario_parameters = next(iter(scenario_param_dict.values())) if scenario_param_dict else []

    campaign_input_files.extend(analysis_files)

    for config in configurations:
        if variation_classes is None:
            # Read variation classes from the variation file
            variation_classes_and_parameters = _get_variation_classes(config, vast_dir)
        else:
            raise NotImplementedError("Passing variation_classes is not implemented yet")

        # Initialize config dict with scenario parameters if they exist
        config_dict = {}

        scenario_parameters = config.get('parameters', [])
        if scenario_parameters:
            # Convert list of single-key dicts to a single dict
            for param in scenario_parameters:
                if isinstance(param, dict):
                    config_dict.update(param)

            # Validate that all specified parameters exist in the scenario
            if existing_scenario_parameters:
                # Extract parameter names from the scenario (each entry has a 'name' field)
                valid_param_names = [p.get('name') for p in existing_scenario_parameters if isinstance(p, dict) and 'name' in p]

                # Check each parameter in config_dict
                invalid_params = [p for p in config_dict if p not in valid_param_names]
                if invalid_params:
                    raise ValueError(
                        f"Invalid parameters in scenario '{config['name']}': {invalid_params}. "
                        f"Valid parameters are: {valid_param_names}"
                    )

        _check_declared_outputs(
            config, variation_classes_and_parameters,
            existing_scenario_parameters, parameters, vast_dir)

        current_configs = [{
            'name': config['name'],
            'config': config_dict}]

        for variation_class, variation_parameters in variation_classes_and_parameters:
            started_at = datetime.now(timezone.utc).isoformat()
            t0 = time.monotonic()
            # Auxiliary container: if the plugin declares one, the active backend
            # (local docker or cluster sidecar) provides a runner for its use.
            container_spec = variation_class.get_required_container(variation_parameters)
            container_runner = _make_container_runner(container_spec)
            try:
                result, var_input_files, var_campaign_transient, var_config_transient = execute_variation(os.path.dirname(variation_file), current_configs, variation_class,
                                                                                                          variation_parameters, general_parameters, progress_update_callback, scenario_file, output_dir,
                                                                                                          container_runner=container_runner)
            except VariationInfeasibleError as exc:
                # Name the config block here -- neither execute_variation nor the plugin
                # knows it, but it is exactly what a reader needs to act on the message
                # (which config, not just which plugin/why), whether this propagates
                # (batch mode) or is only logged before the config is dropped (search).
                named_exc = VariationInfeasibleError(
                    f"config '{config['name']}': {exc}", config_name=config['name'])
                if not tolerate_infeasible:
                    raise named_exc from exc
                # This parameter draw cannot be realized (e.g. ObstacleVariation lost its
                # placement budget) -- drop just this one config rather than aborting every
                # other config in the batch. Only opted into by search composition
                # (Compose.compose), where a bad draw is an expected, probabilistic outcome
                # rather than a sweep-design error.
                progress_update_callback(f"Variation pipeline stopped at {variation_class.__name__} - {named_exc}")
                current_configs = []
                break
            finally:
                if container_runner is not None:
                    container_runner.close()
            duration = round(time.monotonic() - t0, 3)

            # Validate and collect variation input files
            for vf in var_input_files:
                _validate_relative_path(vf, f"variation {variation_class.__name__} input file")
            campaign_input_files.extend(var_input_files)

            # Collect transient files from this variation step
            campaign_transient_files.extend(var_campaign_transient)
            config_transient_files.extend(var_config_transient)

            if result is None or len(result) == 0:
                # If a variation step fails or produces no results, stop the pipeline
                progress_update_callback(f"Variation pipeline stopped at {variation_class.__name__} - no configs to process")
                current_configs = []
                break
            else:
                logger.debug(f"Variation result after {variation_class.__name__}: \n{pformat(result)}")

            # Record variation execution data on each resulting config
            variation_entry = {
                "name": variation_class.__name__,
                "started_at": started_at,
                "duration": duration,
            }
            for c in result:
                if "_variations" not in c:
                    c["_variations"] = []
                entry = dict(variation_entry)
                # Let variation plugins add extra fields to the _variations entry
                extras = c.pop("_variation_entry_extras", None)
                if extras and isinstance(extras, dict):
                    entry.update(extras)
                c["_variations"].append(entry)

            current_configs = result

        for c in current_configs:
            c["_config_name"] = config.get("name")
            c["_config_block"] = config

        configs.extend(current_configs)

    # Normalize _config_files and _config_transient_files: convert artifact absolute
    # paths (those inside output_dir) to paths relative to output_dir.  This makes
    # cached and non-cached results structurally identical, and lets execution.py
    # resolve paths via campaign_data["_output_dir"] instead of relying on
    # whichever absolute path happened to be used during generation.
    _abs_output = os.path.abspath(output_dir)
    _norm_prefix = _abs_output + os.sep
    for cfg in configs:
        for field in ("_config_files", "_config_transient_files"):
            entries = cfg.get(field)
            if not entries:
                continue
            normalized = []
            for rel, path in entries:
                abs_path = os.path.abspath(path)
                if abs_path.startswith(_norm_prefix):
                    path = os.path.relpath(abs_path, _abs_output)
                else:
                    # Source file – keep absolute for portability
                    path = abs_path
                normalized.append((rel, path))
            cfg[field] = normalized

    # Resolve each configuration's simulator settings, now that the configurations exist,
    # and stage the union of the worlds they name. `run_files` is still being built at this
    # point, so the additions travel with the campaign and are hashed into every
    # configuration's identity exactly as the campaign-level world already was.
    _resolve_config_sim_blocks(configs, parameters, vast_dir, run_files,
                               existing_scenario_parameters)

    # Extract execution parameters from execution section
    #
    # A simulator backend's contributions are merged in *here*, once, so everything
    # downstream -- the container plan, the image builds, the run environment -- reads
    # one already-complete picture instead of each re-asking the backend and risking a
    # different answer. The campaign always wins; a backend fills in what was left out.
    from robovast.common.simulators import apply_backend  # pylint: disable=import-outside-toplevel
    execution_section = apply_backend(parameters.get('execution', {}) or {},
                                      base_dir=os.path.dirname(variation_file))
    # And immediately resolve the ``family:`` refs a backend (or the default) contributed,
    # so the campaign data carries concrete images from here on. The project/tag arrive as
    # arguments rather than being read from the environment here: this runs in the
    # service's campaign worker thread, and for a ``plugins:``-declaring config in a
    # subprocess of it, while the service composes campaigns for several projects at once.
    from robovast.common.execution import \
        resolve_family_images_in_containers  # pylint: disable=import-outside-toplevel
    resolve_family_images_in_containers(execution_section.get('containers'),
                                        project=image_project, tag=image_project_tag)
    execution_params = {
        "env": execution_section.get('env'),
        "run_as_user": execution_section.get('run_as_user'),
        # Every container the run starts. This is a *whitelist*, not a copy: both lanes
        # read only what is listed here, so a key omitted below never arrives and the
        # campaign runs as if it were unset.
        "containers": execution_section.get('containers'),
        "local": execution_section.get('local'),
        # `runs` is what the campaign's size is reported in (validate, `config info`,
        # preview_configurations all read it here). It was missing, so every campaign was reported as
        # one run per configuration -- a 25-trial sweep looked like 5 -- right where an agent decides
        # whether it can afford to start.
        "runs": execution_section.get('runs', 1),
        "runs_per_job": execution_section.get('runs_per_job', 1),
        "simulation": execution_section.get('simulation'),
        "mode": execution_section.get('mode'),
        # Environment the backend supplies (see apply_backend). Kept separate from
        # ``env`` so precedence stays explicit where it is applied: a campaign's own
        # execution.env wins over it.
        "_backend_env": execution_section.get('_backend_env'),
    }

    # Build result dictionary
    result = {
        "vast": variation_file,
        "scenario_file": scenario_file,
        "configs": configs,
        "_run_files": run_files,
        # How the derived inputs were made: generator, params, the files it read with
        # their hashes, what it wrote. Dumped into <campaign>/_transient/configurations.yaml,
        # so a campaign records the provenance of inputs it did not author.
        "_generated": generated_records,
        "_input_files": campaign_input_files,
        "_transient_files": campaign_transient_files,
        "_output_dir": os.path.abspath(output_dir),
        "execution": execution_params,
        "created_at": datetime.now().isoformat()
    }

    # Add metadata if it exists
    metadata = parameters.get('metadata')
    if metadata:
        result["metadata"] = metadata

    # --- Store result in cache ---
    if _cache_meta is not None and _cache_key is not None:
        try:
            # Same JSON-safe transform the isolated-compose boundary uses (tuples →
            # tagged dicts, ephemeral fields stripped), so a cached campaign matches a
            # subprocess-composed one byte-for-byte.
            _cache_meta.set_json(_cache_key, _result_to_transport(result))
            logger.debug("Stored generate_scenario_variations metadata in cache")

            # Archive the entire output_dir as a single tarball.
            # On cache hit the whole folder is extracted, no per-file bookkeeping needed.
            if output_dir and os.path.isdir(output_dir):
                tar_path = _cache_artifacts.get_path(_cache_key)
                with tarfile.open(tar_path, "w:gz") as tar:
                    tar.add(output_dir, arcname="")
                _cache_artifacts.set_from_path(_cache_key)
                logger.debug("Stored output_dir tarball in cache: %s", tar_path)

        except Exception as e:  # pylint: disable=broad-except
            logger.warning("Failed to cache generate_scenario_variations result: %s", e)

    return result
