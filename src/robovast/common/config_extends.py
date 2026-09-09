# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``extends:`` — a ``.vast`` built on another, resolved before anything reads the campaign.

Campaigns that differ in a few lines are otherwise written by copying a neighbour, and what
drifts is never the part anyone meant to change: a container's resources, a postprocessing
step, a dashboard. ``extends:`` lets the shared part be stated once.

**Resolution is total and happens at load.** The merged mapping is what validates, what
composes, and what is hashed into a configuration's identity — so rewriting a flat campaign
onto a base leaves every ``config_identifier`` unchanged, and its results stay comparable with
what it produced before. Nothing downstream knows this key existed.

Pure ``dict -> dict`` over YAML and paths: it must never import :mod:`robovast.common.config`,
for the same reason a migration step must not. It runs *before* the models exist, and a base
is a fragment rather than a document -- it has no ``execution:`` and would not validate alone.
"""

import copy
import os
from pathlib import Path

from robovast.common import yaml_strict

#: The key itself, consumed here and never seen by the schema.
EXTENDS_KEY = "extends"


def _deep_merge(base, over):
    """*over* laid on *base*: mappings merge at every depth, everything else replaces.

    **Lists replace rather than append**, which is the rule the sim channel already applies to
    a list leaf, so one rule covers the whole file. It is also the rule people meet first: a
    child adding one panel restates the panel list. Appending cannot be the default and be
    undone -- a campaign that meant to replace a list would silently run both -- whereas an
    opt-in append can be added later without changing what any existing file means.
    """
    if not isinstance(base, dict) or not isinstance(over, dict):
        return copy.deepcopy(over)
    merged = copy.deepcopy(base)
    for key, value in over.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _resolve_base(raw, config_path, project_dir):
    """The base *config_path* names, as an absolute path. ``None`` when it names none.

    Refused rather than advised when it escapes the project directory: only the project
    directory reaches a service workspace, so a base outside it composes from the tree in
    place and is simply absent on the cluster lane. A generator input that escapes is an
    advisory because the campaign merely reads it; a base *is* the campaign.
    """
    ext = raw.get(EXTENDS_KEY)
    if ext is None:
        return None
    if not isinstance(ext, str) or not ext.strip():
        raise ValueError(
            f"{config_path}: 'extends' must be a path to another .vast file, relative to this "
            f"one; got {ext!r}.")
    resolved = Path(os.path.abspath(os.path.join(os.path.dirname(config_path), ext)))
    if os.path.commonpath([str(resolved), str(project_dir)]) != str(project_dir):
        raise ValueError(
            f"{config_path}: 'extends' names {ext!r}, which resolves to {resolved} — outside "
            f"the campaign's project directory ({project_dir}). Only the project directory is "
            f"copied into a service workspace, so a base outside it is absent wherever the "
            f"campaign is not run from this tree. Move the base under the project directory.")
    if not resolved.exists():
        raise ValueError(f"{config_path}: 'extends' names {ext!r}, which does not exist "
                         f"({resolved}).")
    return resolved


def _walk(config_path, project_dir, seen, raw=None):
    """``(merged, chain)`` for *config_path*, bases first. *chain* is nearest-last."""
    config_path = Path(os.path.abspath(config_path))
    if raw is None:
        loaded = yaml_strict.load(config_path.read_text(), path=config_path) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"{config_path} must be a mapping at the top level.")
        raw = loaded
    base_path = _resolve_base(raw, config_path, project_dir)
    if base_path is None:
        return copy.deepcopy(raw), [config_path]
    if base_path in seen:
        chain = " -> ".join(str(p) for p in (*seen, base_path))
        raise ValueError(f"'extends' cycle detected: {chain}")
    base, chain = _walk(base_path, project_dir, seen | {config_path})
    _check_versions(base, raw, base_path, config_path)
    merged = _deep_merge(base, raw)
    merged.pop(EXTENDS_KEY, None)
    return merged, [*chain, config_path]


def _check_versions(base, child, base_path, config_path):
    """A base that declares a version must agree with the file extending it.

    Inheriting a version silently is the worst reading of a disagreement: the campaign would
    be validated against one schema and authored against another, and nothing says so.
    """
    base_version, child_version = base.get("version"), child.get("version")
    if base_version is None or child_version is None or base_version == child_version:
        return
    raise ValueError(
        f"{config_path} declares config version {child_version} but the base it extends, "
        f"{base_path}, declares {base_version}. A base and the campaign extending it are one "
        f"document and must agree; drop the version from the base, or bring both to the same.")


def resolve_extends(config, config_path):
    """*config* with its ``extends:`` chain merged in and the key removed.

    Returned untouched, and identical, when the file declares no ``extends:`` — which is the
    case for every campaign that does not opt in.
    """
    if not isinstance(config, dict) or config.get(EXTENDS_KEY) is None:
        return config
    project_dir = Path(os.path.abspath(os.path.dirname(config_path)))
    merged, _chain = _walk(config_path, project_dir, frozenset(), raw=config)
    return merged


def extends_sources(config, config_path):
    """Every file the campaign is composed from, bases first, the campaign itself last.

    What the archive stores verbatim. A campaign declaring no ``extends:`` yields just itself,
    so the caller has one rule rather than two.
    """
    config_path = Path(os.path.abspath(config_path))
    if not isinstance(config, dict) or config.get(EXTENDS_KEY) is None:
        return [config_path]
    project_dir = Path(os.path.abspath(os.path.dirname(config_path)))
    _merged, chain = _walk(config_path, project_dir, frozenset(), raw=config)
    return chain
