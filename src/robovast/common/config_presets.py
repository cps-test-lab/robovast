# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``configuration_presets:`` — reusable configuration bodies, composed by ``use:``.

A factorial design is written as its axes rather than as its cross product: a robot, a stack
and a trial are three presets, and each cell names the three it is. The alternative people
reach for first is YAML merge keys, and it is not merely awkward -- ``<<:`` is shallow, so
where several presets contribute to one subtree all but the first vanish **with no error**, and
the campaign runs its full sweep against values nobody wrote.

**Precedence, lowest to highest**::

    preset parameters  <  parameters  <  preset variations  <  variations

Two axes rather than four rules: preset before entry, and fixed before variation. The second is
not new -- a variation has always won over the fixed value it varies -- so presets split each
existing layer in two and change no existing precedence. The consequence worth knowing is that
an entry's ``parameters:`` does **not** beat a preset's ``variations:``; to override a preset's
sweep, declare your own variation on that destination.

Pure ``dict -> dict``: never imports :mod:`robovast.common.config`, because it runs before the
models exist.
"""

import copy
import logging

from robovast.common.config_channels import CHANNELS, SIM, channel
from robovast.common.simulators import flatten_sim_block, unflatten_sim_block

logger = logging.getLogger(__name__)

PRESETS_KEY = "configuration_presets"
USE_KEY = "use"


def _flat(name, block):
    """A channel's values as flat destination -> value, so collisions compare like with like.

    ``sim:`` is nested and may spell one destination two ways (``overrides.a.b`` or the nested
    mapping); flattening is what makes those the same key rather than two that never meet.
    """
    return flatten_sim_block(block) if name == SIM else dict(block or {})


def _variation_destinations(variation):
    """Every ``<channel>:<destination>`` one variation entry writes, for collision checks.

    A variation is a single-key mapping of plugin name to its parameters, and the channel keys
    inside name where its outputs land -- as a bare string for a single output, or as a
    slot -> destination mapping for a plugin that produces several.
    """
    if not isinstance(variation, dict) or len(variation) != 1:
        return set()
    (_plugin, params), = variation.items()
    if not isinstance(params, dict):
        return set()
    out = set()
    for name in CHANNELS:
        bound = params.get(name)
        if isinstance(bound, str):
            out.add(f"{name}:{bound}")
        elif isinstance(bound, dict):
            out.update(f"{name}:{dest}" for dest in bound.values() if isinstance(dest, str))
        elif isinstance(bound, list):
            out.update(f"{name}:{dest}" for dest in bound if isinstance(dest, str))
    return out


def _merge_presets(names, presets, config_name):
    """The ``parameters`` and ``variations`` the named presets contribute, in ``use:`` order.

    Two presets writing one destination is refused rather than resolved by position. They are
    meant to be orthogonal axes, so a collision leaves which one won decided by the order of a
    list two lines away, and the losing write leaves no trace anywhere -- which is the failure
    this whole mechanism exists to make impossible. A collision between a preset and the
    configuration's own block is a different thing entirely and is allowed silently: the entry
    is more specific than the axes it composes, and the override is visible where the reader is
    already looking.
    """
    merged_channels = {name: {} for name in CHANNELS}
    owner = {}
    variations = []
    seen_destinations = {}

    for preset_name in names:
        preset = presets[preset_name]
        for name in CHANNELS:
            for destination, value in _flat(name, channel(preset, name)).items():
                previous = owner.get((name, destination))
                if previous is not None and previous != preset_name:
                    raise ValueError(
                        f"configuration {config_name!r}: presets {previous!r} and "
                        f"{preset_name!r} both set the {name}: destination {destination!r}. "
                        "Two presets writing one destination leaves which of them won decided "
                        "by the order of use:, and the other write is recorded nowhere. Set it "
                        "in the configuration itself, which wins over every preset, or split "
                        "the presets so one owns the destination.")
                owner[(name, destination)] = preset_name
                merged_channels[name][destination] = value
        for variation in preset.get("variations") or []:
            for destination in _variation_destinations(variation):
                previous = seen_destinations.get(destination)
                if previous is not None and previous != preset_name:
                    raise ValueError(
                        f"configuration {config_name!r}: presets {previous!r} and "
                        f"{preset_name!r} both vary {destination!r}. Two presets sweeping one "
                        "destination is a factor crossed with itself, whose cells cannot be "
                        "told apart. Keep the sweep in one preset.")
                seen_destinations[destination] = preset_name
            variations.append(copy.deepcopy(variation))

    return merged_channels, variations, seen_destinations


def _apply(entry, merged_channels, preset_variations, preset_destinations):
    """Lay *entry*'s own values over what its presets contributed."""
    for name in CHANNELS:
        own = _flat(name, channel(entry, name))
        if own:
            merged_channels[name].update(own)

    own_variations = list(entry.get("variations") or [])
    own_destinations = set()
    for variation in own_variations:
        own_destinations |= _variation_destinations(variation)

    # A preset variation the entry re-declares is replaced, not crossed with: sweeping one
    # destination twice produces cells that differ in nothing a reader could name.
    kept = []
    for variation in preset_variations:
        replaced = _variation_destinations(variation) & own_destinations
        if replaced:
            logger.info(
                "configuration %r: its own variation over %s replaces the one preset %r "
                "contributed", entry.get("name"), ", ".join(sorted(replaced)),
                preset_destinations.get(sorted(replaced)[0]))
            continue
        kept.append(variation)
    variations = kept + own_variations

    parameters = {}
    for name in CHANNELS:
        block = merged_channels[name]
        if block:
            parameters[name] = unflatten_sim_block(block) if name == SIM else block
    resolved = {k: v for k, v in entry.items() if k not in (USE_KEY, "parameters", "variations")}
    if parameters:
        resolved["parameters"] = parameters
    if variations:
        resolved["variations"] = variations
    return resolved


def expand_configuration_presets(config):
    """*config* with every ``use:`` resolved and ``configuration_presets:`` removed.

    Returned untouched, and identical, when the file declares no presets and no ``use:`` --
    which is every campaign that does not opt in.
    """
    if not isinstance(config, dict):
        return config
    presets = config.get(PRESETS_KEY)
    entries = config.get("configuration") or []
    uses = [e for e in entries if isinstance(e, dict) and e.get(USE_KEY)]
    if presets is None and not uses:
        return config
    presets = presets or {}

    known = ", ".join(sorted(presets)) or "(none defined)"
    resolved_entries = []
    used_names = set()
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get(USE_KEY):
            resolved_entries.append(entry)
            continue
        names = entry[USE_KEY]
        config_name = entry.get("name")
        if not isinstance(names, list):
            raise ValueError(f"configuration {config_name!r}: use: must be a list of preset "
                             f"names. Defined presets: {known}.")
        seen = set()
        for preset_name in names:
            if preset_name in seen:
                raise ValueError(
                    f"configuration {config_name!r}: use: names {preset_name!r} twice. A preset "
                    "applied again changes nothing, so this is either a copy or an attempt to "
                    "reorder precedence, and neither has a meaning here.")
            seen.add(preset_name)
            if preset_name not in presets:
                raise ValueError(
                    f"configuration {config_name!r}: use: names preset {preset_name!r}, which "
                    f"this file does not define. Defined presets: {known}.")
        used_names |= seen
        merged, preset_variations, destinations = _merge_presets(names, presets, config_name)
        resolved_entries.append(_apply(entry, merged, preset_variations, destinations))

    for unused in sorted(set(presets) - used_names):
        # Advisory, not a refusal: `vast configuration export-configs` copies the whole
        # document and replaces only `configuration`, so refusing here would turn an existing
        # command into one that emits files nothing can load.
        logger.warning(
            "configuration_presets: %r is defined and never used -- no configuration lists it "
            "under use:. Presets in use: %s", unused,
            ", ".join(sorted(used_names)) or "(none)")

    out = {k: v for k, v in config.items() if k != PRESETS_KEY}
    if entries:
        out["configuration"] = resolved_entries
    return out
