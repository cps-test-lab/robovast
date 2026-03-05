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

"""Merge campaign-dirs with identical configs into one merged output."""

import hashlib
import logging
import shutil
from pathlib import Path
from typing import Iterator

import yaml

logger = logging.getLogger(__name__)


def _iter_campaign_configs(results_dir: str) -> Iterator[tuple[str, str, Path, str | None]]:
    """Iterate over campaign-dirs and config-dirs, yielding (campaign, config_name, config_path, config_identifier).

    Skips config-dirs without config.yaml (yields config_identifier=None).
    """
    root = Path(results_dir)
    if not root.is_dir():
        return

    for run_item in sorted(root.iterdir()):
        if not run_item.is_dir() or not run_item.name.startswith("campaign-"):
            continue
        if run_item.name == "_config":
            continue
        campaign = run_item.name

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
                logger.debug("Skipping config-dir without config.yaml: %s/%s", campaign, config_name)

            yield campaign, config_name, config_item, config_identifier


def merge_results(results_dir: str, merged_campaign_dir: str) -> tuple[bool, str]:
    """Merge campaign-dirs with identical configs into merged_campaign_dir.

    Groups campaign-dir/config-dir by config_identifier from config.yaml.
    Run folders (0, 1, 2, ...) from all campaigns are renumbered and copied.
    Original campaigns are not modified.

    Args:
        results_dir: Source directory containing campaign-* dirs.
        merged_campaign_dir: Output directory for merged results.

    Returns:
        Tuple (success, message).
    """
    # Discover and group by (config_identifier, config_name)
    groups: dict[tuple[str | None, str], list[tuple[str, Path, list[Path]]]] = {}

    for campaign, config_name, config_path, config_identifier in _iter_campaign_configs(results_dir):
        # Skip configs without identifier
        if config_identifier is None:
            continue

        # Collect run folders (0, 1, 2, ...)
        run_paths = []
        for item in sorted(config_path.iterdir()):
            if item.is_dir() and item.name.isdigit():
                run_paths.append(item)

        key = (config_identifier, config_name)
        if key not in groups:
            groups[key] = []
        groups[key].append((campaign, config_path, sorted(run_paths, key=lambda p: int(p.name))))

    if not groups:
        return False, "No config-dirs with config.yaml found to merge."

    # Build merged output - remove existing so runs are idempotent
    merged_base = Path(merged_campaign_dir).resolve()
    results_path = Path(results_dir).resolve()
    if merged_base == results_path:
        return False, (
            f"merged_campaign_dir must not equal results_dir (would delete source): "
            f"{merged_campaign_dir}"
        )

    # Compute a deterministic pseudo campaign-id from the sorted source run ids so the
    # output mirrors the results/ layout: merged_campaign_dir/campaign-<pseudo id>/...
    all_source_campaigns = sorted({campaign for sources in groups.values() for campaign, _, _ in sources})
    pseudo_id = hashlib.sha256("|".join(all_source_campaigns).encode()).hexdigest()[:8]
    pseudo_campaign_dir = f"campaign-{pseudo_id}"
    merged_path = merged_base / pseudo_campaign_dir

    if merged_path.exists():
        shutil.rmtree(merged_path)
    merged_path.mkdir(parents=True, exist_ok=True)

    # Use first run for run-level files
    first_campaign, first_config_path, _ = next(iter(groups.values()))[0]
    first_campaign_path = Path(results_dir) / first_campaign

    # Copy run-level files from first run (files live in _config/, _transient/, _execution/ subdirs)
    run_level_files = [
        ("_config", "scenario.osc"),
        ("_execution", "execution.yaml"),
        ("_transient", "entrypoint.sh"),
        ("_transient", "secondary_entrypoint.sh"),
        ("_transient", "collect_sysinfo.py"),
    ]
    for subdir, fname in run_level_files:
        src = first_campaign_path / subdir / fname
        if src.exists():
            dst_dir = merged_path / subdir
            dst_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst_dir / fname)

    # Copy vast file (stored in _config/)
    for f in (first_campaign_path / "_config").iterdir():
        if f.suffix == ".vast":
            dst_dir = merged_path / "_config"
            dst_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dst_dir / f.name)
            break

    # Copy run-level _config from first run
    src_config = first_campaign_path / "_config"
    if src_config.exists():
        dst_config = merged_path / "_config"
        shutil.copytree(src_config, dst_config, dirs_exist_ok=True)

    # Merge each config group
    # Disambiguate when same config_name appears with different config_identifiers (avoids overwriting)
    used_config_names: set[str] = set()
    total_runs = 0
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

        # Collect and copy run folders, renumbering
        all_runs: list[tuple[str, str, Path]] = []
        for campaign, config_path, run_paths in sources:
            for tp in run_paths:
                all_runs.append((campaign, tp.name, tp))
        all_runs.sort(key=lambda x: (x[0], int(x[1])))

        for idx, (campaign, _, src_run_path) in enumerate(all_runs):
            dst_run = merged_config_dir / str(idx)
            shutil.copytree(src_run_path, dst_run, dirs_exist_ok=True)
            total_runs += 1

    return True, f"Merged {len(groups)} config(s), {total_runs} run(s) into {merged_campaign_dir}/{pseudo_campaign_dir}"
