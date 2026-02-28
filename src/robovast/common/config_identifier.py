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

"""Config identifier computation for merge-results.

Hashes inputs that affect config generation to produce a unique identifier.
Identifiers are stored in config.yaml per config-dir for merge-results grouping.
"""

import hashlib
import importlib
import inspect
import os
from functools import lru_cache
from typing import Any

import yaml


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


def hash_test_files(vast_dir: str, test_file_paths: list[str]) -> str:
    """Hash each test file's content (path + content), sorted by path.

    Args:
        vast_dir: Base directory for resolving relative paths.
        test_file_paths: List of relative paths to test files.

    Returns:
        12-char hex digest combining all file hashes.
    """
    hasher = hashlib.sha256()
    for rel_path in sorted(test_file_paths):
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


def _hash_variation_entrypoints_impl(variation_type_names: list[str]) -> str:
    """Hash the source of every module in the package of each variation entry point."""
    eps_by_name = {}
    try:
        eps = list(importlib.metadata.entry_points(group="robovast.variation_types"))
        for ep in eps:
            if ep.name in variation_type_names:
                eps_by_name[ep.name] = ep
    except Exception:
        pass

    ep_hashes = {}
    for name in sorted(variation_type_names):
        if name not in eps_by_name:
            # Unknown variation type - hash the name to contribute to identifier
            ep_hashes[name] = hashlib.sha256(name.encode()).hexdigest()[:12]
            continue
        ep = eps_by_name[name]
        module_name = ep.value.split(":")[0]
        top_package = module_name.split(".")[0]

        try:
            package = importlib.import_module(top_package)
            package_path = inspect.getfile(package)
            package_dir = os.path.dirname(package_path)

            hasher = hashlib.sha256()
            for path in _iter_package_files(package_dir):
                with open(path, "rb") as f:
                    hasher.update(path.encode())
                    hasher.update(f.read())
            ep_hashes[name] = hasher.hexdigest()[:12]
        except Exception:
            ep_hashes[name] = hashlib.sha256(name.encode()).hexdigest()[:12]

    combined = ",".join(f"{k}={v}" for k, v in sorted(ep_hashes.items()))
    return hashlib.sha256(combined.encode()).hexdigest()[:12]


@lru_cache(maxsize=64)
def hash_variation_entrypoints(variation_type_names: tuple[str, ...]) -> str:
    """Hash variation entry points used in config. Cached by frozenset of names."""
    return _hash_variation_entrypoints_impl(list(variation_type_names))


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


def _collect_paths_from_config(config_block: dict, vast_dir: str) -> set[str]:
    """Recursively extract string values from config block that exist as paths."""
    paths = set()

    def walk(obj):
        if isinstance(obj, str):
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
    test_files_hash: str,
    scenario_file_hash: str,
    variation_type_names: list[str],
) -> tuple[str, dict[str, str]]:
    """Compute unique config identifier from all inputs that affect config generation.

    Args:
        vast_dir: Directory containing the vast file.
        config_block: Configuration entry from vast (name, parameters, variations).
        test_files_hash: Precomputed hash of test_files_filter files.
        scenario_file_hash: Precomputed hash of scenario file content.
        variation_type_names: List of variation type names used in this config.

    Returns:
        Tuple of (12-char hex digest, dict of sub-identifiers for debugging).
    """
    canonical = _canonical_config_block(config_block)
    var_tuple = tuple(sorted(variation_type_names))

    block_hash = _hash_config_block_cached(canonical)
    ref_files_hash = hash_config_referenced_files(vast_dir, canonical)
    var_hash = hash_variation_entrypoints(var_tuple)

    sub_identifier = {
        "block": block_hash,
        "test_files": test_files_hash,
        "scenario_file": scenario_file_hash,
        "config_referenced_files": ref_files_hash,
        "variation_entrypoints": var_hash,
    }

    combined = (
        f"block={block_hash}"
        f",test={test_files_hash}"
        f",scenario={scenario_file_hash}"
        f",ref={ref_files_hash}"
        f",var={var_hash}"
    )
    config_identifier = hashlib.sha256(combined.encode()).hexdigest()[:12]

    return config_identifier, sub_identifier
