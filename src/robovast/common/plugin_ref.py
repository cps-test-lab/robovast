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
from importlib.metadata import entry_points
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


def _load_from_entry_point(name: str, group: str) -> Any:
    eps = entry_points().select(group=group)
    available = {ep.name: ep for ep in eps}
    if name not in available:
        names = ", ".join(sorted(available)) or "(none registered)"
        raise ValueError(
            f"Unknown {group} plugin '{name}'. Available: {names}. "
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
