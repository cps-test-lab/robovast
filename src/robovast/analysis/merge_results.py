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

"""Merge run-dirs with identical configs into one merged output."""

import hashlib
import logging
import shutil
from pathlib import Path
from typing import Iterator

import yaml

logger = logging.getLogger(__name__)


def _iter_run_configs(results_dir: str) -> Iterator[tuple[str, str, Path, str | None]]:
    """Iterate over run-dirs and config-dirs, yielding (run_id, config_name, config_path, config_identifier).

    Skips config-dirs without config.yaml (yields config_identifier=None).
    """
    root = Path(results_dir)
    if not root.is_dir():
        return

    for run_item in sorted(root.iterdir()):
        if not run_item.is_dir() or not run_item.name.startswith("run-"):
            continue
        if run_item.name == "_config":
            continue
        run_id = run_item.name

        for config_item in sorted(run_item.iterdir()):
            if not config_item.is_dir():
                continue
            config_name = config_item.name
            config_yaml_path = config_item / "config.yaml"

            config_identifier = None
            if config_yaml_path.exists():
                try:
                    with open(config_yaml_path, encoding="utf-8") as f:
                        data = yaml.safe_load(f) or {}
                    config_identifier = data.get("config_identifier")
                except Exception as e:
                    logger.warning("Could not read config.yaml from %s: %s", config_yaml_path, e)
            else:
                logger.debug("Skipping config-dir without config.yaml: %s/%s", run_id, config_name)

            yield run_id, config_name, config_item, config_identifier


def merge_results(results_dir: str, merged_run_dir: str) -> tuple[bool, str]:
    """Merge run-dirs with identical configs into merged_run_dir.

    Groups run-dir/config-dir by config_identifier from config.yaml.
    Test folders (0, 1, 2, ...) from all runs are renumbered and copied.
    Original run-dirs are not modified.

    Args:
        results_dir: Source directory containing run-* dirs.
        merged_run_dir: Output directory for merged results.

    Returns:
        Tuple (success, message).
    """
    # Discover and group by (config_identifier, config_name)
    groups: dict[tuple[str | None, str], list[tuple[str, Path, list[Path]]]] = {}

    for run_id, config_name, config_path, config_identifier in _iter_run_configs(results_dir):
        # Skip configs without identifier
        if config_identifier is None:
            continue

        # Collect test folders (0, 1, 2, ...)
        test_paths = []
        for item in sorted(config_path.iterdir()):
            if item.is_dir() and item.name.isdigit():
                test_paths.append(item)

        key = (config_identifier, config_name)
        if key not in groups:
            groups[key] = []
        groups[key].append((run_id, config_path, sorted(test_paths, key=lambda p: int(p.name))))

    if not groups:
        return False, "No config-dirs with config.yaml found to merge."

    # Build merged output - remove existing so runs are idempotent
    merged_base = Path(merged_run_dir).resolve()
    results_path = Path(results_dir).resolve()
    if merged_base == results_path:
        return False, (
            f"merged_run_dir must not equal results_dir (would delete source): "
            f"{merged_run_dir}"
        )

    # Compute a deterministic pseudo run-id from the sorted source run ids so the
    # output mirrors the results/ layout: merged_run_dir/run-<pseudo id>/...
    all_source_run_ids = sorted({run_id for sources in groups.values() for run_id, _, _ in sources})
    pseudo_id = hashlib.sha256("|".join(all_source_run_ids).encode()).hexdigest()[:8]
    pseudo_run_dir = f"run-{pseudo_id}"
    merged_path = merged_base / pseudo_run_dir

    if merged_path.exists():
        shutil.rmtree(merged_path)
    merged_path.mkdir(parents=True, exist_ok=True)

    # Use first run for run-level files
    first_run_id, first_config_path, _ = next(iter(groups.values()))[0]
    first_run_path = Path(results_dir) / first_run_id

    # Copy run-level files from first run
    run_level_files = [
        "scenario.osc",
        "execution.yaml",
        "entrypoint.sh",
        "secondary_entrypoint.sh",
        "collect_sysinfo.py",
    ]
    for fname in run_level_files:
        src = first_run_path / fname
        if src.exists():
            shutil.copy2(src, merged_path / fname)

    # Copy vast file (stored alongside scenario.osc in run dir)
    for f in first_run_path.iterdir():
        if f.suffix == ".vast":
            shutil.copy2(f, merged_path / f.name)
            break

    # Copy run-level _config from first run
    src_config = first_run_path / "_config"
    if src_config.exists():
        dst_config = merged_path / "_config"
        shutil.copytree(src_config, dst_config, dirs_exist_ok=True)

    # Merge each config group
    # Disambiguate when same config_name appears with different config_identifiers (avoids overwriting)
    used_config_names: set[str] = set()
    total_tests = 0
    for (config_identifier, config_name), sources in groups.items():
        output_dir_name = config_name
        if output_dir_name in used_config_names:
            output_dir_name = f"{config_name}_{config_identifier[:8]}"
        used_config_names.add(output_dir_name)

        merged_config_dir = merged_path / output_dir_name
        merged_config_dir.mkdir(parents=True, exist_ok=True)

        # Copy scenario.config, _config, config.yaml from first source
        _, first_config_path, _ = sources[0]
        for fname in ["scenario.config", "config.yaml"]:
            src = first_config_path / fname
            if src.exists():
                shutil.copy2(src, merged_config_dir / fname)
        src_config = first_config_path / "_config"
        if src_config.exists():
            dst_config = merged_config_dir / "_config"
            shutil.copytree(src_config, dst_config, dirs_exist_ok=True)

        # Collect and copy test folders, renumbering
        all_tests: list[tuple[str, str, Path]] = []
        for run_id, config_path, test_paths in sources:
            for tp in test_paths:
                all_tests.append((run_id, tp.name, tp))
        all_tests.sort(key=lambda x: (x[0], int(x[1])))

        for idx, (run_id, _, src_test_path) in enumerate(all_tests):
            dst_test = merged_config_dir / str(idx)
            shutil.copytree(src_test_path, dst_test, dirs_exist_ok=True)
            total_tests += 1

    return True, f"Merged {len(groups)} config(s), {total_tests} test(s) into {merged_run_dir}/{pseudo_run_dir}"
