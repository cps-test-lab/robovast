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

"""Config identifier computation for merge-campaigns.

Hashes inputs that affect config generation to produce a unique identifier.
Identifiers are stored in config.yaml per config-directory for merge-campaigns grouping.
"""

import hashlib
import importlib.machinery
import importlib.metadata
import importlib.util
import os
from functools import lru_cache
from typing import Any

import yaml

from .plugin_ref import is_file_ref


def hash_file_content(file_path: str) -> str:
    """Hash a single file's content.

    Args:
        file_path: Absolute path to the file.

    Returns:
        12-char hex digest of the file content.
    """
    with open(file_path, "rb") as f:
        content = f.read()
    return hashlib.sha256(content).hexdigest()[:12]


def hash_run_files(vast_dir: str, run_file_paths: list[str]) -> str:
    """Hash each run file's content (path + content), sorted by path.

    Args:
        vast_dir: Base directory for resolving relative paths.
        run_file_paths: List of relative paths to run files.

    Returns:
        12-char hex digest combining all file hashes.
    """
    hasher = hashlib.sha256()
    for rel_path in sorted(run_file_paths):
        full_path = os.path.join(vast_dir, rel_path)
        if os.path.isfile(full_path):
            hasher.update(rel_path.encode())
            with open(full_path, "rb") as f:
                hasher.update(f.read())
    return hasher.hexdigest()[:12]


def _iter_package_files(package_path: str) -> list[str]:
    """Yield all .py source files in a package directory."""
    result = []
    for root, _, files in os.walk(package_path):
        for fname in sorted(files):
            if fname.endswith(".py"):
                result.append(os.path.join(root, fname))
    return sorted(result)


class VariationSourceNotFound(LookupError):
    """A variation's source cannot be found, so nothing can stand for it in a hash."""


def variation_refs(config_block: dict) -> list[str]:
    """The variation references a configuration block names, as the ``.vast`` wrote them."""
    listed = config_block.get("variations")
    if not isinstance(listed, list):
        return []
    return [ref for item in listed if isinstance(item, dict) for ref in item]


def _source_root(module_name: str, site_dir: str | None) -> str | None:
    """The first regular package (its directory) or module (its file) on *module_name*'s path.

    Found without importing it. A namespace root (``robovast``) is skipped: it spans
    distributions, so what the variation ships in is below it. With *site_dir*, only that
    directory is searched.
    """
    parts = module_name.split(".")
    search_path = [site_dir] if site_dir else None
    for depth in range(1, len(parts) + 1):
        name = ".".join(parts[:depth])
        if site_dir:
            spec = importlib.machinery.PathFinder.find_spec(name, search_path)
        else:
            spec = importlib.util.find_spec(name)
        if spec is None:
            return None
        if spec.origin not in (None, "namespace"):
            if spec.submodule_search_locations is None:
                return spec.origin
            return os.path.dirname(spec.origin)
        search_path = list(spec.submodule_search_locations or [])
    return None


def _hash_source(root: str) -> str:
    hasher = hashlib.sha256()
    for path in _iter_package_files(root) if os.path.isdir(root) else [root]:
        with open(path, "rb") as f:
            hasher.update(path.encode())
            hasher.update(f.read())
    return hasher.hexdigest()[:12]


def _variation_entry_points(site_dir: str) -> list[tuple[str | None, Any]]:
    """``(site_dir, entry point)`` of each variation type, the workspace's plugins first.

    The workspace's plugin venv is read as metadata, never put on ``sys.path``: a
    ``plugins:`` package is imported only in the isolated compose worker, which leads its
    path with that venv -- hence first here too.
    """
    found = []
    if os.path.isdir(site_dir):
        for dist in importlib.metadata.distributions(path=[site_dir]):
            found.extend((site_dir, ep) for ep in dist.entry_points
                         if ep.group == "robovast.variation_types")
    found.extend((None, ep) for ep in
                 importlib.metadata.entry_points(group="robovast.variation_types"))
    return found


def _hash_variation_entrypoints_impl(refs: list[str], vast_dir: str) -> str:
    """Hash the source of the package each variation reference is shipped in.

    A ``<path>.py:<Class>`` reference contributes its name only: its module is a run file,
    content-hashed there.

    Raises:
        VariationSourceNotFound: a reference no installed package or workspace plugin
            registers, or whose package has no source to hash.
    """
    from robovast.common.config_plugins import \
        plugin_site_dir  # pylint: disable=import-outside-toplevel

    eps_by_name = {}
    for site_dir, ep in _variation_entry_points(plugin_site_dir(vast_dir)):
        eps_by_name.setdefault(ep.name, (site_dir, ep))

    ep_hashes = {}
    for name in sorted(set(refs)):
        if is_file_ref(name):
            ep_hashes[name] = hashlib.sha256(name.encode()).hexdigest()[:12]
            continue
        if name not in eps_by_name:
            raise VariationSourceNotFound(
                f"Variation type '{name}' is registered by no installed package and no "
                f"workspace plugin, so its source cannot be part of the configuration's "
                f"identity.")
        site_dir, ep = eps_by_name[name]
        root = _source_root(ep.value.split(":")[0], site_dir)
        if root is None:
            raise VariationSourceNotFound(
                f"Variation type '{name}' ({ep.value}) resolves to no package source, so "
                f"its source cannot be part of the configuration's identity.")
        ep_hashes[name] = _hash_source(root)

    combined = ",".join(f"{k}={v}" for k, v in sorted(ep_hashes.items()))
    return hashlib.sha256(combined.encode()).hexdigest()[:12]


def _plugin_stamp(vast_dir: str) -> int | None:
    """Changes whenever the workspace's plugins are (re)installed."""
    from robovast.common.config_plugins import (  # pylint: disable=import-outside-toplevel
        MARKER_NAME, plugin_dir)
    try:
        return os.stat(os.path.join(plugin_dir(vast_dir), MARKER_NAME)).st_mtime_ns
    except FileNotFoundError:
        return None


@lru_cache(maxsize=64)
def _hash_variation_entrypoints_cached(refs: tuple[str, ...], vast_dir: str,
                                       _stamp: int | None) -> str:
    return _hash_variation_entrypoints_impl(list(refs), vast_dir)


def hash_variation_entrypoints(refs, vast_dir: str) -> str:
    """Hash the source of the variations *refs* name.

    Raises:
        VariationSourceNotFound: see :func:`_hash_variation_entrypoints_impl`.
    """
    vast_dir = os.path.abspath(vast_dir)
    return _hash_variation_entrypoints_cached(
        tuple(sorted(set(refs))), vast_dir, _plugin_stamp(vast_dir))


def hash_read_files(vast_dir: str, paths) -> str:
    """Hash the files a configuration's variations read, by content.

    A file inside *vast_dir* is named by its relative path, so a campaign composed again
    from its archived copy keeps its identity.
    """
    vast_dir = os.path.abspath(vast_dir)
    hasher = hashlib.sha256()
    for path in sorted(set(paths)):
        rel = os.path.relpath(path, vast_dir)
        hasher.update((path if rel.startswith("..") else rel).encode())
        with open(path, "rb") as f:
            hasher.update(f.read())
    return hasher.hexdigest()[:12]


def _canonical_config_block(config_block: dict) -> str:
    """Serialize config block to canonical YAML for hashing."""
    return yaml.dump(config_block, default_flow_style=False, sort_keys=True)


@lru_cache(maxsize=128)
def _hash_config_block_cached(canonical_yaml: str) -> str:
    """Hash configuration block. Cached by canonical YAML string."""
    return hashlib.sha256(canonical_yaml.encode()).hexdigest()[:12]


def hash_config_block(config_block: dict) -> str:
    """Hash configuration block. Uses cached implementation."""
    canonical = _canonical_config_block(config_block)
    return _hash_config_block_cached(canonical)


def collect_paths_from_config(config_block: dict, vast_dir: str) -> set[str]:
    """Recursively extract string values from config block that resolve to existing paths."""
    return _collect_paths_from_config(config_block, vast_dir)


def _collect_paths_from_config(config_block: dict, vast_dir: str) -> set[str]:
    """Recursively extract string values from config block that exist as paths."""
    paths = set()

    def walk(obj):
        if isinstance(obj, str):
            # A blank string is not a path reference. ``os.path.join(vast_dir, "")`` is
            # ``vast_dir`` itself, which always exists — so an empty config value (a
            # deliberate one, e.g. the empty package name that makes ros_launch take a
            # plain file path) collected the whole project directory as a "referenced
            # file" and made the hash walk every file under it. On a campaign directory
            # that is both ruinously slow and outright fatal: the walk raced a run's
            # transient files and died on FileNotFoundError for a path that existed when
            # os.walk listed it.
            if not obj.strip():
                return
            full = os.path.join(vast_dir, obj)
            if os.path.exists(full):
                paths.add(obj)
        elif isinstance(obj, dict):
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(config_block)
    return paths


def _hash_path_content(vast_dir: str, rel_path: str, hasher: Any) -> None:
    """Hash a file or directory content, updating hasher in place."""
    full_path = os.path.join(vast_dir, rel_path)
    if os.path.isfile(full_path):
        hasher.update(rel_path.encode())
        with open(full_path, "rb") as f:
            hasher.update(f.read())
    elif os.path.isdir(full_path):
        for root, _, files in os.walk(full_path):
            for fname in sorted(files):
                file_path = os.path.join(root, fname)
                rel = os.path.relpath(file_path, vast_dir)
                hasher.update(rel.encode())
                with open(file_path, "rb") as f:
                    hasher.update(f.read())


def _hash_config_referenced_files_impl(vast_dir: str, config_block: dict) -> str:
    """Hash files/dirs referenced in config block."""
    paths = _collect_paths_from_config(config_block, vast_dir)
    hasher = hashlib.sha256()
    for rel_path in sorted(paths):
        _hash_path_content(vast_dir, rel_path, hasher)
    return hasher.hexdigest()[:12]


@lru_cache(maxsize=64)
def hash_config_referenced_files(vast_dir: str, canonical_config_yaml: str) -> str:
    """Hash config-referenced files. Cached by (vast_dir, canonical config YAML)."""
    config_block = yaml.safe_load(canonical_config_yaml)
    return _hash_config_referenced_files_impl(vast_dir, config_block)


def compute_config_identifier(
    vast_dir: str,
    config_block: dict,
    run_files_hash: str,
    scenario_file_hash: str,
    variations: list[str],
    sut_sources_hash: str = "",
    read_files=(),
) -> tuple[str, dict[str, str]]:
    """Compute unique config identifier from all inputs that affect config generation.

    Args:
        vast_dir: Directory containing the vast file.
        config_block: Configuration entry from vast (name, parameters, variations).
        run_files_hash: Precomputed hash of run_files files.
        scenario_file_hash: Precomputed hash of scenario file content.
        variations: The variation references the config block names
            (:func:`variation_refs`).
        sut_sources_hash: Content hash of the config files the ``sut:`` channel
            addresses, or ``""`` when the campaign declares none. They are inputs
            to generation exactly as a world is, but they cannot ride in
            ``run_files_hash``: that list is also *staged*, and a source mounted
            un-rewritten beside its rewritten copy is what the channel refuses.
        read_files: Absolute paths of the files the config's variations read beyond what
            the block names (``Variation.get_read_files``) -- the image a map YAML names.

    Returns:
        Tuple of (12-char hex digest, dict of sub-identifiers for debugging).
    """
    canonical = _canonical_config_block(config_block)

    block_hash = _hash_config_block_cached(canonical)
    ref_files_hash = hash_config_referenced_files(vast_dir, canonical)
    var_hash = hash_variation_entrypoints(variations, vast_dir)

    sub_identifier = {
        "block": block_hash,
        "run_files": run_files_hash,
        "scenario_file": scenario_file_hash,
        "config_referenced_files": ref_files_hash,
        "variation_entrypoints": var_hash,
    }

    combined = (
        f"block={block_hash}"
        f",run={run_files_hash}"
        f",scenario={scenario_file_hash}"
        f",ref={ref_files_hash}"
        f",var={var_hash}"
    )
    # Appended only when the campaign uses the channel. Folding an empty component in
    # unconditionally would change the identifier of every campaign already archived, and
    # `merge_results` groups by it -- so a re-run would silently stop matching its own
    # stored results.
    if sut_sources_hash:
        sub_identifier["sut_sources"] = sut_sources_hash
        combined += f",sut={sut_sources_hash}"
    # Same reason: appended only when a variation read a file.
    if read_files:
        read_hash = hash_read_files(vast_dir, read_files)
        sub_identifier["read_files"] = read_hash
        combined += f",read={read_hash}"
    config_identifier = hashlib.sha256(combined.encode()).hexdigest()[:12]

    return config_identifier, sub_identifier
