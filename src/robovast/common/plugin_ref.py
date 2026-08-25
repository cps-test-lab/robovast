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

"""Resolve a plugin reference to a class — by entry-point name or local file.

A reference is either:

* an **entry-point name** registered in a ``robovast.*`` group (installed
  plugins, the default), or
* a **local file** ``<path>.py:<ClassName>`` resolved relative to a base
  directory (typically the ``.vast`` file's directory) and imported directly.

This lets users drop project-specific search/extraction logic next to their
``.vast`` without packaging it. The same resolver is shared by the search
plugins and by results postprocessing.
"""

import hashlib
import importlib.util
import logging
import os
import sys
from importlib.metadata import distributions, entry_points
from typing import Any

logger = logging.getLogger(__name__)

# A file reference looks like ``some/path.py:ClassName``.
_FILE_REF_MARKER = ".py:"


def is_file_ref(ref: str) -> bool:
    """Whether ``ref`` is a ``<path>.py:<ClassName>`` file reference."""
    return _FILE_REF_MARKER in ref


def _load_from_file(ref: str, base_dir: str) -> Any:
    rel_path, _, class_name = ref.partition(":")
    if not class_name:
        raise ValueError(
            f"File plugin reference '{ref}' must be '<path>.py:<ClassName>'")
    path = rel_path if os.path.isabs(rel_path) else os.path.join(base_dir or ".", rel_path)
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Plugin file not found for reference '{ref}': {path}")

    # Use a stable, unique module name so repeated loads don't clash. A
    # deterministic digest of the absolute path keeps the name reproducible
    # across processes (unlike hash(), which is seed-randomized).
    digest = hashlib.sha1(path.encode("utf-8")).hexdigest()[:12]  # nosec B324 - not security
    mod_name = "robovast_plugin_" + os.path.splitext(os.path.basename(path))[0] + "_" + digest
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load plugin module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    if not hasattr(module, class_name):
        raise AttributeError(f"'{class_name}' not found in plugin file {path}")
    return getattr(module, class_name)


def _self_import_root() -> str:
    """The top-level import package this module belongs to (``robovast``)."""
    return __name__.partition(".")[0]


def _is_builtin_group(group: str) -> bool:
    """Whether *group* is one robovast registers itself, so empty means broken."""
    return group.startswith(_self_import_root() + ".")


def _diagnose_empty_builtin_group(group: str) -> str:
    """Why a group robovast itself registers came back empty, or ``""``.

    Empty is an ordinary answer for a third-party group --- nobody installed a provider.
    For a group robovast declares in its own ``pyproject.toml`` it cannot happen in a
    healthy installation, so the honest report is "this installation is broken", not
    "that plugin is unknown". The generic advice to run ``poetry install`` actively
    misleads here: it sends the reader to fix something that is not wrong.

    The failure this exists for: ``importlib.metadata`` deduplicates distributions **by
    name** and keeps the first on ``sys.path``, so a second distribution called
    ``robovast`` anywhere ahead of the real one replaces its entry points wholesale ---
    every group at once, not merely the name being looked up. That is what a workspace
    plugin directory containing its own copy of the host used to produce, and reading
    ``(none registered)`` gives no way to guess it.

    Diagnosis only. Deliberately *not* a fallback that unions the shadowed distribution's
    entry points back in: that would leave a process running a mixture of two robovasts
    and report success.
    """
    if not _is_builtin_group(group):
        return ""
    root = _self_import_root()
    lines = [f"{root} itself registers this group, so an empty one means a broken "
             f"installation rather than a missing plugin."]
    try:
        from robovast.common.config_plugins import (  # noqa: PLC0415 - avoid import cycle
            PLUGIN_DIRNAME, canonical_name)
        # distributions() walks sys.path in order, so the first match IS the one
        # entry_points() kept -- the others are invisible to every lookup in this process.
        same = [d for d in distributions()
                if canonical_name(d.metadata["Name"] or "") == canonical_name(root)]
        if len(same) > 1:
            where = [str(d.locate_file("")) for d in same]
            lines.append(
                f"{len(same)} distributions named {root!r} are on sys.path and only the "
                f"first is used: {where[0]} (in use) shadows {', '.join(where[1:])}.")
        elif same:
            lines.append(f"The {root!r} distribution in use is {same[0].locate_file('')}.")
        workspace = [p for p in sys.path
                     if PLUGIN_DIRNAME in p.split(os.sep) and os.path.isdir(p)
                     and any(e.startswith(f"{root}-") and e.endswith(".dist-info")
                             for e in os.listdir(p))]
        if workspace:
            lines.append(
                f"A workspace plugin directory on sys.path carries its own {root}: "
                f"{', '.join(workspace)}. Remove it and restart the service --- the "
                f"sys.path entry and already-imported modules outlive the files.")
        elif len(same) <= 1:
            lines.append(f"No shadowing copy found, so {root}'s own installed metadata is "
                         f"missing or stale: reinstall it ('make venv').")
    except Exception as e:  # noqa: BLE001 - a diagnosis must never replace the real error
        logger.debug("could not diagnose empty group %r: %s", group, e)
    return " ".join(lines)


def _load_from_entry_point(name: str, group: str) -> Any:
    eps = entry_points().select(group=group)
    available = {ep.name: ep for ep in eps}
    if name not in available:
        names = ", ".join(sorted(available)) or "(none registered)"
        message = f"Unknown {group} plugin '{name}'. Available: {names}. "
        diagnosis = _diagnose_empty_builtin_group(group) if not available else ""
        if diagnosis:
            raise ValueError(message + diagnosis)
        raise ValueError(
            message +
            f"Use a '<path>.py:<Class>' file reference for a local module, or run "
            f"'poetry install' to register installed plugins.")
    return available[name].load()


def load_ref(ref: str, group: str, base_dir: str = "") -> Any:
    """Resolve ``ref`` to a class via entry-point ``group`` or a local file.

    Args:
        ref: An entry-point name, or a ``<path>.py:<ClassName>`` file reference.
        group: The entry-point group to search for name references.
        base_dir: Directory that file references resolve against (the ``.vast``
            directory).
    """
    if is_file_ref(ref):
        return _load_from_file(ref, base_dir)
    return _load_from_entry_point(ref, group)


def list_ref_names(group: str) -> set:
    """Names of every plugin registered in entry-point ``group``.

    Best-effort: returns an empty set (rather than raising) if entry points can't
    be read, so callers can union it with a static built-in set for validation.
    """
    try:
        names = {ep.name for ep in entry_points().select(group=group)}
    except Exception:  # noqa: BLE001 - enumeration must never break validation
        logger.debug("could not enumerate entry-point group %r", group)
        return set()
    if not names:
        # Validation reports "unknown strategy" against a shadowed process too, and its
        # caller has no other way to learn that the emptiness is the installation's fault.
        diagnosis = _diagnose_empty_builtin_group(group)
        if diagnosis:
            logger.warning("entry-point group %r is empty. %s", group, diagnosis)
    return names
