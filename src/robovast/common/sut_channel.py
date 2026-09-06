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

"""The ``sut:`` channel: how the system under test is configured, per configuration.

The counterpart of :mod:`robovast.common.simulators`' ``sim``-block helpers, for the third
surface a campaign can vary. A destination is ``<source>.<path>``: *source* names a config
file the campaign declared (or the reserved ``env``), and *path* is addressed in that
file's own syntax by its format.

**A block on this channel is always flat.** ``sim:`` accepts a nested mapping and flattens
it; this one cannot, and it is the single place the two channels genuinely differ. The part
after the source name belongs to the format and may be an XPath -- ``//RoundRobin[@name='x']``
is not expressible as nested keys under any reading -- so a block is
``{"<source>.<path>": value}``, one string key, split exactly once.
"""

import copy
import logging
import os
from dataclasses import dataclass, field

from .config import (CONTAINER_ROLES, RESERVED_ENV_NAMES, SCENARIO_CONTAINER,
                     SIMULATION_CONTAINER, SUT_CONTAINER)
from .sut_formats import CannotAnswer, resolve_format

logger = logging.getLogger(__name__)

#: The reserved source name for the environment. Not declared in ``config_files:`` and not
#: a format: a format loads a file, reports what it contains and writes it back, and every
#: one of those is degenerate for an environment variable. It is the channel's *second
#: carrier*, not another kind of document.
ENV_SOURCE = "env"

#: Where a configuration's resolved ``sut`` block is recorded, beside ``sim.config``. A
#: record, not an input: the copies the cell actually runs are staged and mounted, and this
#: sits next to the configuration so what the stack was given is readable without diffing
#: two documents.
SUT_CONFIG_FILE = "sut.config"

#: The value meaning *this node is not there*, as opposed to present and empty -- a
#: distinction a stack makes and no assignment expresses.
#:
#: A single-key mapping under a ``$``-prefixed name because it has to survive every
#: transport: the config schema, ``preview_configurations``, the web editor and MCP are all
#: JSON, so a YAML tag would die at the first boundary. ``null`` would silently reinterpret
#: a value that is occasionally legitimate and means nothing in XML; a bare string sentinel
#: collides with a legitimate string. ``$`` is the JSON-Schema idiom for *reserved, not
#: data*, and is not a character a ROS parameter name can contain.
ABSENT = "$absent"

#: Which channel already owns a defined role's configuration, for the refusal message. Only
#: the wording lives here -- what is *refused* is decided from :data:`CONTAINER_ROLES`, so a
#: role added later fails closed rather than being permitted by a list nobody extended.
_OTHER_CHANNELS = {
    SIMULATION_CONTAINER: "sim: (the simulator's own configuration)",
    SCENARIO_CONTAINER: "scenario: (a parameter the .osc declares)",
}


def is_absent(value) -> bool:
    """Whether *value* is the absence marker rather than something to write."""
    return isinstance(value, dict) and len(value) == 1 and value.get(ABSENT) is True


class SutChannelError(ValueError):
    """A campaign's use of the channel is wrong, and says so naming what is available."""


@dataclass
class Source:
    """One declared configuration file: where it is, and what reads it."""
    name: str
    rel_path: str
    abs_path: str
    fmt: object
    container: str


@dataclass
class SutContribution:
    """What one configuration's resolved block produces.

    Two carriers behind one result, so composition consumes one thing regardless of which
    a cell's factors landed on: ``files`` are staged per configuration and mounted, ``env``
    is merged into the environment for that cell's job.
    """
    files: list = field(default_factory=list)   #: ``(deploy-relative path, absolute path)``
    env: dict = field(default_factory=dict)     #: ``{name: value}``; ``None`` means unset


def split_destination(destination: str):
    """``"nav2.a.b"`` -> ``("nav2", "a.b")``. Split once: the rest is the format's."""
    text = str(destination)
    source, sep, remainder = text.partition(".")
    if not sep or not source or not remainder:
        raise SutChannelError(
            f"'{text}' is not a sut: destination: write '<source>.<path>', where <source> "
            "is a file declared under execution.containers.<name>.config_files (or the "
            "reserved 'env')")
    return source, remainder


def declared_sources(execution: dict, vast_dir: str) -> dict:
    """Every config source the campaign declares, ``{name: Source}``.

    Sources belong to the containers that *implement* the system under test -- the ``sut``
    role and any ad-hoc container beside it. A declaration on any *other* defined role is
    refused, naming the channel that already owns that surface.

    **A source name is unique across the campaign**, not per container: a destination names
    a source and nothing else, so the container a source is declared on says who owns and
    reads the file rather than scoping its name.
    """
    sources: dict = {}
    for container, block in ((execution or {}).get("containers") or {}).items():
        declared = (block or {}).get("config_files") if isinstance(block, dict) else None
        if not declared:
            continue
        if container in CONTAINER_ROLES and container != SUT_CONTAINER:
            owner = _OTHER_CHANNELS.get(container, "another channel")
            raise SutChannelError(
                f"container '{container}' declares config_files, but its configuration is "
                f"addressed by {owner}. The sut: channel is for the containers that "
                "implement the system under test.")
        for name, entry in declared.items():
            if name == ENV_SOURCE:
                raise SutChannelError(
                    f"'{ENV_SOURCE}' is a reserved source name -- it addresses the "
                    "environment, which is not a file and is never declared")
            if name in sources:
                raise SutChannelError(
                    f"source '{name}' is declared on both '{sources[name].container}' and "
                    f"'{container}'; a destination names a source, so the name must be "
                    "unique across the campaign")
            if isinstance(entry, dict):
                rel, fmt_name = entry.get("file", ""), entry.get("format", "")
            else:
                rel, fmt_name = str(entry), ""
            if not rel:
                raise SutChannelError(f"source '{name}' declares no file")
            # ONE SOURCE PER FILE, as well as one file per source. Each source is loaded and
            # written back as its own document, and two sources sharing a file write to one
            # staged path -- so the second dump replaces the first and every destination on
            # the losing source silently does nothing. A file addressed under two names has
            # one spelling that works, so the second name is refused rather than picked.
            clash = next((other for other in sources.values() if other.rel_path == rel), None)
            if clash is not None:
                raise SutChannelError(
                    f"sources '{clash.name}' and '{name}' both declare '{rel}'. One source "
                    "per file: each is rewritten as a whole document, so a second name for "
                    f"one file loses one of the two. Address it as '{clash.name}.<path>'.")
            sources[name] = Source(
                name=name, rel_path=rel, abs_path=os.path.join(vast_dir, rel),
                fmt=resolve_format(rel, fmt_name), container=container)
    return sources


def source_paths(execution: dict, vast_dir: str) -> list:
    """The ``.vast``-relative path of every declared source, for content hashing.

    These files are inputs to configuration generation exactly as a world is, so editing
    one has to change a configuration's identity -- otherwise a re-run reuses an expansion
    built from the previous content, silently. They cannot travel in ``run_files`` to get
    that for free, because ``run_files`` also *stages* what it hashes.

    Best effort: a campaign whose declaration is malformed is refused elsewhere, with a
    message about the declaration rather than about hashing.
    """
    try:
        return [source.rel_path for source in declared_sources(execution, vast_dir).values()]
    except Exception:  # noqa: BLE001 - a bad declaration is refused by the real check
        return []


def resolve_sut_path(sources: dict, destination: str):
    """``(Source or None, path)`` for *destination*; ``None`` for the environment.

    Refuses a destination naming no declared source, listing the ones that exist -- the
    counterpart of :func:`~robovast.common.simulators.resolve_sim_path` refusing a key the
    backend does not have.
    """
    name, path = split_destination(destination)
    if name == ENV_SOURCE:
        # The same names `execution.env` refuses. This carrier reaches the run's
        # environment by a different route, so the guard has to be applied here too --
        # otherwise the channel would be a way around a rule the other route enforces, and
        # a campaign could quietly repoint something RoboVAST sets for itself.
        if path in RESERVED_ENV_NAMES:
            raise SutChannelError(
                f"'{destination}' writes '{path}', which RoboVAST sets itself and a "
                "campaign may not override. Reserved names: "
                + ", ".join(sorted(RESERVED_ENV_NAMES)))
        return None, path
    if name not in sources:
        known = ", ".join(sorted(sources)) or "(none declared)"
        raise SutChannelError(
            f"'{destination}' names source '{name}', which this campaign does not declare. "
            f"Declared sources: {known}. Declare it under "
            "execution.containers.<name>.config_files, or write 'env.<NAME>'.")
    return sources[name], path


def check_destinations(execution: dict, vast_dir: str, destinations) -> None:
    """Refuse a destination the declaring file cannot have written at it.

    Each source is parsed once. A format that cannot decide leaves its destinations
    unchecked and **says so at warning level** -- never at debug, which reads exactly like
    a check that passed.
    """
    sources = declared_sources(execution, vast_dir)
    docs: dict = {}
    for destination in destinations:
        source, path = resolve_sut_path(sources, destination)
        if source is None:
            continue
        if source.name not in docs:
            docs[source.name] = source.fmt.load(source.abs_path)
        try:
            addressable = source.fmt.can_address(docs[source.name], path)
        except CannotAnswer as exc:
            logger.warning(
                "sut: destination '%s' was not pre-checked (%s). It is still refused when "
                "the configuration is built if it is wrong.", destination, exc)
            continue
        if not addressable:
            listing = source.fmt.addresses(docs[source.name])
            detail = ""
            if listing:
                detail = " Addressable there: " + ", ".join(sorted(listing)[:20])
            raise SutChannelError(
                f"'{destination}' addresses nothing in {source.rel_path}.{detail}")


def merge_sut_block(authored: dict, values: dict) -> dict:
    """The configuration's fixed block, then what variations wrote over it.

    Same precedence the other two channels have: a factor's value wins over the fixed one
    it varies.
    """
    merged = dict(authored or {})
    merged.update(values or {})
    return merged


def materialize(execution: dict, vast_dir: str, block: dict, out_dir: str,
                config_name: str) -> SutContribution:
    """Write one configuration's rewritten config files, and collect its environment.

    Destinations are applied in the order the block carries them, which is the order the
    variations ran -- so two factors writing into one file compose predictably.
    """
    sources = declared_sources(execution, vast_dir)
    contribution = SutContribution()
    touched: dict = {}
    for destination, value in (block or {}).items():
        source, path = resolve_sut_path(sources, destination)
        if source is None:
            contribution.env[path] = None if is_absent(value) else value
            continue
        if source.name not in touched:
            touched[source.name] = (source, source.fmt.load(source.abs_path))
        _source, doc = touched[source.name]
        if is_absent(value):
            _source.fmt.remove(doc, path)
        else:
            _source.fmt.set(doc, path, copy.deepcopy(value))

    # EVERY declared source is staged, not only the ones this configuration's factors reached.
    # The original is dropped from `run_files` for the campaign as a whole, so a configuration that
    # happens to address nothing in a file would otherwise reach its containers with no copy of that
    # file at all -- and the failure surfaces in the stack as a missing path, far from the
    # declaration that caused it. It also makes the mount uniform: every cell of a campaign sees the
    # file at the same place whether or not that cell varied it.
    for source in sources.values():
        if source.name not in touched:
            touched[source.name] = (source, source.fmt.load(source.abs_path))

    for source, doc in touched.values():
        written = os.path.join(out_dir, config_name, source.rel_path)
        os.makedirs(os.path.dirname(written), exist_ok=True)
        source.fmt.dump(doc, written)
        contribution.files.append((source.rel_path, written))
    return contribution
