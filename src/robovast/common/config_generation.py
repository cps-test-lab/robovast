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

import copy
import fnmatch
import logging
import os
import re
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from importlib.metadata import entry_points
from pprint import pformat

from .common import convert_dataclasses_to_dict, get_scenario_parameters, load_config
from .config_identifier import collect_paths_from_config, hash_variation_entrypoints
from .file_cache2 import CacheKey, FileCache2

logger = logging.getLogger(__name__)


def progress_update(msg):
    logger.info(msg)


def execute_variation(base_dir, configs, variation_class, parameters, general_parameters, progress_update_callback, scenario_file, output_dir=None):
    logger.debug(f"Executing variation: {variation_class.__name__}")
    variation = variation_class(base_dir, parameters, general_parameters, progress_update_callback, scenario_file, output_dir)

    # Collect input files for campaign self-containment
    input_files = variation.get_input_files()

    try:
        configs = variation.variation(copy.deepcopy(configs))
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


def _get_variation_classes(scenario_config):
    """
    Read variation class names scenario

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
                else:
                    error_msg = f"Unknown variation class '{class_name}' found in variation file.\n"
                    if not available_classes:
                        error_msg += "No variation plugins are registered. This usually means the robovast package is not properly installed.\n"
                        error_msg += "To fix this, run: poetry install\n"
                        error_msg += "If you're in a CI environment, ensure 'poetry install' (without --no-root) has been executed."
                    else:
                        error_msg += f"Available variation types: {', '.join(available_classes.keys())}"
                    raise ValueError(error_msg)

    return variation_classes


def _validate_relative_path(path, description="path"):
    """Validate that a path is relative and does not escape its base directory."""
    if os.path.isabs(path):
        raise ValueError(f"{description} must be relative, got absolute path: {path}")
    normalized = os.path.normpath(path)
    if normalized.startswith('..'):
        raise ValueError(f"{description} must not escape the base directory: {path}")


def _collect_analysis_input_files(parameters, base_dir=None):
    """Collect file paths referenced in evaluation.visualization and results.postprocessing sections."""
    analysis_files = []

    # Collect visualization files from evaluation section
    evaluation = parameters.get('evaluation')
    if isinstance(evaluation, dict):
        visualizations = evaluation.get('visualization') or []
    elif evaluation is not None and hasattr(evaluation, 'visualization'):
        visualizations = evaluation.visualization or []
    else:
        visualizations = []

    # Collect postprocessing files from results_processing section.
    # The top-level config key is ``results_processing`` (not ``results``).
    data = parameters.get('results_processing') or parameters.get('results')
    if isinstance(data, dict):
        postprocessing = data.get('postprocessing') or []
    elif data is not None and hasattr(data, 'postprocessing'):
        postprocessing = data.postprocessing or []
    else:
        postprocessing = []

    for viz_entry in visualizations:
        if isinstance(viz_entry, dict):
            for _plugin_name, plugin_config in viz_entry.items():
                if isinstance(plugin_config, dict):
                    for _key, path in plugin_config.items():
                        if isinstance(path, str) and (path.endswith('.ipynb') or path.endswith('.py')):
                            analysis_files.append(path)

    # Collect any postprocessing plugin parameter value that refers to an existing file
    for pp_entry in postprocessing:
        if not isinstance(pp_entry, dict):
            continue
        for _plugin_name, plugin_params in pp_entry.items():
            if not isinstance(plugin_params, dict):
                continue
            for _key, value in plugin_params.items():
                if not isinstance(value, str) or os.path.isabs(value):
                    continue
                # Only collect if the path actually resolves to an existing file
                candidate = os.path.join(base_dir, value) if base_dir else value
                if os.path.isfile(candidate):
                    analysis_files.append(value)

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
_CACHE_FORMAT_VERSION = 2


def _build_generate_cache_key(
    variation_file: str,
    vast_dir: str,
    scenario_file: str,
    run_files: list,
    analysis_files: list,
    configurations: list,
) -> CacheKey:
    """Build a FileCache2 CacheKey covering every input that affects generate_scenario_variations.

    All ``add_file`` calls pass *base_dir=vast_dir* so that the key uses
    ``relpath(file, vast_dir)`` rather than basename, preventing collisions
    between different files that share the same name.
    """
    key = CacheKey()

    # Cache format version — bumped whenever the stored structure changes.
    key.add("cache_format_version", _CACHE_FORMAT_VERSION)

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


def _rebuild_variation_gui_classes(configurations: list) -> dict:
    """Cheaply reconstruct variation_gui_classes from config blocks without running variations."""
    gui_classes = {}
    for config_block in configurations:
        for variation_class, _ in _get_variation_classes(config_block):
            gui_class = getattr(variation_class, 'GUI_CLASS', None)
            renderer_class = getattr(variation_class, 'GUI_RENDERER_CLASS', None)
            if gui_class:
                if gui_class not in gui_classes:
                    gui_classes[gui_class] = []
                if renderer_class and renderer_class not in gui_classes[gui_class]:
                    gui_classes[gui_class].append(renderer_class)
    return gui_classes


def generate_scenario_variations(variation_file, progress_update_callback=None, variation_classes=None, output_dir=None, use_cache=True):
    """Generate all scenario variation configs from a .vast file.

    Caching is active for all flows when ``use_cache=True``.  Two cache
    entries are stored under ``<vast_dir>/.cache/``:

    * ``config_generation_{key}.json`` — config metadata.  Per-config
      ``_config_files`` are stored as relative paths only.
    * ``config_generation_artifacts_{key}.tar.gz`` — artifact files that
      variation plugins wrote into ``output_dir`` (only created when
      ``_config_files`` is non-empty, e.g. for FloorplanVariation).

    On a cache hit the metadata is returned immediately.  If an
    ``output_dir`` was requested and artifact files were cached, they are
    extracted into that directory and ``_config_files`` is reconstructed
    with absolute paths pointing there.
    """
    if not progress_update_callback:
        progress_update_callback = logger.debug
    progress_update_callback("Start generating configs.")

    parameters = load_config(variation_file)

    # Get scenario file from configuration section
    configurations = parameters.get('configuration', [])

    run_files = []
    # Get run_files patterns from config
    run_files_patterns = parameters.get("execution", {}).get("run_files", [])
    if run_files_patterns:
        additional_run_files = collect_filtered_files(run_files_patterns, os.path.dirname(variation_file))
        progress_update_callback(f"Loaded {len(run_files_patterns)} run_files patterns (found {len(additional_run_files)} files).")
        run_files.extend(additional_run_files)

    vast_dir = os.path.abspath(os.path.dirname(variation_file))

    # Get scenario_file from execution section (resolved early for cache key)
    execution_scenario_file_name = parameters.get('execution', {}).get('scenario_file')

    # Validate scenario_file path
    if execution_scenario_file_name:
        _validate_relative_path(execution_scenario_file_name, "execution.scenario_file")

    scenario_file = os.path.join(os.path.dirname(variation_file), execution_scenario_file_name) if execution_scenario_file_name else None

    if scenario_file is None:
        raise ValueError("No scenario_file specified in execution section of the variation file. Please add 'scenario_file' to the execution section.")

    # Collect analysis notebook files (resolved early for cache key)
    analysis_files = _collect_analysis_input_files(parameters, base_dir=os.path.dirname(variation_file))
    for af in analysis_files:
        _validate_relative_path(af, "analysis file")

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
        )
        _cached = _cache_meta.get_json(_cache_key)
        if _cached is not None:
            logger.info("Cache HIT for generate_scenario_variations (%s)", variation_file)
            # Restore artifact files if an output_dir was requested
            _artifacts_path = _cache_artifacts.get_path(_cache_key)
            _have_artifacts = (
                output_dir is not None
                and os.path.exists(_artifacts_path)
                and _cache_artifacts.get(_cache_key, content=False) is not None
            )
            if _have_artifacts:
                os.makedirs(output_dir, exist_ok=True)
                with tarfile.open(_artifacts_path, "r:gz") as tar:
                    tar.extractall(output_dir)  # nosec – trusted local cache
                logger.debug("Restored artifact files to %s from cache", output_dir)
            # Reconstruct (rel, abs) tuples in _config_files for each config.
            # Source files: abs is stored directly in the cache entry.
            # Artifact files: abs is reconstructed from the caller's output_dir
            #   (files were extracted from the artifact tar above).
            for cfg in _cached.get("configs", []):
                rebuilt = []
                for entry in cfg.get("_config_files", []):
                    rel = entry["rel"]
                    if entry["kind"] == "source":
                        rebuilt.append((rel, entry["abs"]))
                    else:  # artifact
                        if output_dir:
                            rebuilt.append((rel, os.path.abspath(os.path.join(output_dir, rel))))
                cfg["_config_files"] = rebuilt
            progress_update_callback("Loaded configurations from cache (no changes detected).")
            return _cached, _rebuild_variation_gui_classes(configurations)
        logger.info("Cache MISS for generate_scenario_variations (%s)", variation_file)
    else:
        _cache_meta = None
        _cache_artifacts = None
        _cache_key = None

    configs = []
    variation_gui_classes = {}
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
            variation_classes_and_parameters = _get_variation_classes(config)
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

        current_configs = [{
            'name': config['name'],
            'config': config_dict}]

        for variation_class, variation_parameters in variation_classes_and_parameters:
            variation_gui_class = None
            if hasattr(variation_class, 'GUI_CLASS'):
                if variation_class.GUI_CLASS is not None:
                    variation_gui_class = variation_class.GUI_CLASS
            if variation_gui_class:
                if variation_gui_class not in variation_gui_classes:
                    variation_gui_classes[variation_gui_class] = []
            variation_gui_renderer_class = None
            if hasattr(variation_class, 'GUI_RENDERER_CLASS'):
                variation_gui_renderer_class = variation_class.GUI_RENDERER_CLASS
                if variation_gui_renderer_class is not None:
                    if variation_gui_class is None:
                        raise ValueError(f"Variation class {variation_class.__name__} has GUI_RENDERER_CLASS defined but no GUI_CLASS.")
                    variation_gui_classes[variation_gui_class].append(variation_gui_renderer_class)
            started_at = datetime.now(timezone.utc).isoformat()
            t0 = time.monotonic()
            result, var_input_files, var_campaign_transient, var_config_transient = execute_variation(os.path.dirname(variation_file), current_configs, variation_class,
                                                                                                      variation_parameters, general_parameters, progress_update_callback, scenario_file, output_dir)
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
            c["_config_block"] = config

        configs.extend(current_configs)

    # Extract execution parameters from execution section
    execution_section = parameters.get('execution', {})
    execution_params = {
        "env": execution_section.get('env'),
        "run_as_user": execution_section.get('run_as_user'),
        "image": execution_section.get('image'),
        "resources": execution_section.get('resources'),
        "secondary_containers": execution_section.get('secondary_containers'),
        "local": execution_section.get('local'),
    }

    # Build result dictionary
    result = {
        "vast": variation_file,
        "scenario_file": scenario_file,
        "configs": configs,
        "_run_files": run_files,
        "_input_files": campaign_input_files,
        "_transient_files": campaign_transient_files,
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
            cacheable = copy.deepcopy(result)
            # Strip campaign-level fields with absolute, run-specific paths.
            cacheable["_transient_files"] = []

            # For _config_files, split into:
            #   - source files  (abs NOT under output_dir): store {rel, abs, kind}; the
            #     abs path is stable (lives in vast_dir or project) so it's safe to cache.
            #   - artifact files (abs IS under output_dir): store {rel, kind}; the actual
            #     file is packaged into the companion .tar.gz and extracted on cache hit.
            # Both rel paths must be relative — raise immediately if not.
            artifact_entries = []  # (rel, abs_path) for tar packaging
            norm_output = (os.path.abspath(output_dir) + os.sep) if output_dir else None
            for cfg in cacheable.get("configs", []):
                cfg.pop("_config_transient_files", None)
                cfg.pop("_config_block", None)
                raw_files = cfg.get("_config_files", [])
                storable = []
                for rel, abs_path in raw_files:
                    if os.path.isabs(rel):
                        raise ValueError(
                            f"_config_files entry has an absolute relative path '{rel}' "
                            f"in config '{cfg.get('name')}'. "
                            "Variation plugins must use relative paths in _config_files."
                        )
                    is_artifact = bool(norm_output and os.path.abspath(abs_path).startswith(norm_output))
                    if is_artifact:
                        storable.append({"rel": rel, "kind": "artifact"})
                        artifact_entries.append((rel, abs_path))
                    else:
                        storable.append({"rel": rel, "abs": abs_path, "kind": "source"})
                cfg["_config_files"] = storable

            _cache_meta.set_json(_cache_key, convert_dataclasses_to_dict(cacheable))
            logger.debug("Stored generate_scenario_variations metadata in cache")

            # Package artifact files into a tar.gz when variations wrote any.
            # Use the original abs_path (not output_dir/rel) since artifacts may live
            # in subdirectories of output_dir (e.g. output_dir/<floorplan_name>/maps/).
            # arcname = rel so they are extracted to the right place on cache hit.
            if artifact_entries and output_dir:
                tar_path = _cache_artifacts.get_path(_cache_key)
                with tarfile.open(tar_path, "w:gz") as tar:
                    seen = set()
                    for rel, abs_path in artifact_entries:
                        if rel in seen:
                            continue
                        seen.add(rel)
                        if os.path.exists(abs_path):
                            tar.add(abs_path, arcname=rel)
                        else:
                            logger.warning("Artifact file not found, skipping: %s", abs_path)
                _cache_artifacts.set_from_path(_cache_key)
                logger.debug("Stored %d artifact file(s) in cache tar", len(seen))

        except Exception as e:  # pylint: disable=broad-except
            logger.warning("Failed to cache generate_scenario_variations result: %s", e)

    return result, variation_gui_classes
