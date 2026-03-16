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

"""Publication functionality for run result data.

Publication plugins operate on the full results directory (parent of
campaign-* dirs) and are responsible for packaging or distributing results,
for example by creating zip archives.

Plugin interface
----------------
Each publication plugin is a callable with the signature::

    def plugin(
        results_dir: str,
        config_dir: str,
        **params,
    ) -> Tuple[bool, str]:
        ...

where *results_dir* is the results directory (parent of campaign-* dirs),
*config_dir* is the campaign directory, and *params* are the
plugin-specific keyword arguments taken from the configuration.  The return
value is a ``(success, message)`` tuple.

Configuration format
--------------------

.. code-block:: yaml

   results_processing:
     publication:
       - zip:
           exclude_filter:
           - "*.pyc"
           include_filter:
           - "*.csv"
           destination: archives/
"""

import inspect
import os
from importlib.metadata import entry_points
from typing import Callable, Dict, List, Optional, Tuple

from omegaconf import OmegaConf

from robovast.common.results_utils import find_campaign_config


def load_publication_plugins() -> Dict[str, Callable]:
    """Load publication plugins from entry points.

    Returns:
        Dictionary mapping plugin names to their callable functions.
    """
    plugins: Dict[str, Callable] = {}
    try:
        eps = entry_points(group='robovast.publication_plugins')
        for ep in eps:
            try:
                plugin_obj = ep.load()
                if inspect.isclass(plugin_obj):
                    plugin_obj = plugin_obj()
                plugins[ep.name] = plugin_obj
            except Exception as e:
                print(f"Warning: Failed to load publication plugin '{ep.name}': {e}")
    except Exception:
        pass
    return plugins


def get_publication_config(config_path: str) -> List:
    """Read the publication configuration from a Hydra config file.

    Args:
        config_path: Path to the config YAML file.

    Returns:
        List of publication entries or empty list if none are defined.
    """
    cfg = OmegaConf.load(config_path)
    config_dict = OmegaConf.to_container(cfg, resolve=True)
    results_processing = config_dict.get("results_processing", {})
    if results_processing is None:
        return []
    entries = results_processing.get("publication", [])
    return entries if entries else []


def _execute_plugin(
    plugin_name: str,
    plugin_func: Callable,
    params: dict,
    results_dir: str,
    config_dir: str,
    config_path: Optional[str] = None,
    force: bool = False,
) -> Tuple[bool, str]:
    """Execute a single publication plugin.

    Args:
        plugin_name: Plugin name (for error messages).
        plugin_func: The plugin callable.
        params: Keyword arguments for the plugin.
        results_dir: Results directory path.
        config_dir: Campaign directory path.
        config_path: Absolute path to the resolved config file.
        force: When ``True``, inject ``overwrite=True`` into *params*.

    Returns:
        Tuple of (success, message).
    """
    try:
        effective_params = dict(params)
        if force:
            effective_params.setdefault("overwrite", True)
        if config_path is not None:
            effective_params.setdefault("_config_file", config_path)
        result = plugin_func(results_dir=results_dir, config_dir=config_dir, **effective_params)
        if isinstance(result, (list, tuple)) and len(result) >= 2:
            return result[0], result[1]
        return bool(result), ""
    except TypeError as e:
        return False, f"Plugin '{plugin_name}' argument error: {e}"
    except Exception as e:
        return False, f"Plugin '{plugin_name}' execution error: {e}"


def run_publication(
    results_dir: str,
    output_callback=None,
    config_file: Optional[str] = None,
    force: bool = False,
) -> Tuple[bool, str]:
    """Run all publication plugins defined in the configuration.

    Args:
        results_dir: Directory containing run results (parent of campaign-* dirs).
        output_callback: Optional callable for status messages.
        config_file: Optional explicit path to a config file. When given,
            the campaign copy is ignored.
        force: When ``True``, pass ``overwrite=True`` to every plugin.

    Returns:
        Tuple of (success, message).
    """
    def output(msg: str) -> None:
        if output_callback:
            output_callback(msg)
        else:
            print(msg)

    if not os.path.exists(results_dir):
        return False, f"Results directory does not exist: {results_dir}"

    if config_file is not None:
        if not os.path.isfile(config_file):
            return False, f"Override config file does not exist: {config_file}"
        config_path = os.path.abspath(config_file)
        campaign_dir = os.path.dirname(os.path.dirname(config_path))
        output(f"Using override config: {config_path}")
    else:
        config_path, campaign_dir = find_campaign_config(results_dir)
        if config_path is None:
            return False, (
                f"No .hydra/config.yaml found in any campaign directory under: {results_dir}\n"
                "Ensure at least one execution campaign has been completed."
            )
        output(f"Using config from: {config_path}")

    entries = get_publication_config(config_path)
    if not entries:
        return True, "No publication entries defined."

    plugins = load_publication_plugins()

    success = True
    total = len(entries)
    for i, entry in enumerate(entries, 1):
        if isinstance(entry, str):
            plugin_name = entry
            params: dict = {}
        elif isinstance(entry, dict):
            if len(entry) != 1:
                output(f"[{i}/{total}] Invalid publication entry: dict must have exactly one key")
                success = False
                continue
            plugin_name = next(iter(entry))
            params = entry[plugin_name] or {}
            if not isinstance(params, dict):
                output(f"[{i}/{total}] Invalid params for '{plugin_name}': must be a dict")
                success = False
                continue
        else:
            output(f"[{i}/{total}] Invalid publication entry type: {type(entry)}")
            success = False
            continue

        if plugin_name not in plugins:
            available = ', '.join(sorted(plugins.keys()))
            output(
                f"[{i}/{total}] Unknown publication plugin '{plugin_name}'. "
                f"Available: {available if available else 'none'}"
            )
            success = False
            continue

        display = f"{plugin_name} (params: {params})" if params else plugin_name
        output(f"[{i}/{total}] Executing: {display}")

        ok, msg = _execute_plugin(
            plugin_name=plugin_name,
            plugin_func=plugins[plugin_name],
            params=params,
            results_dir=results_dir,
            config_dir=campaign_dir,
            config_path=config_path,
            force=force,
        )
        if ok:
            output(f"\u2713 {msg}")
        else:
            output(f"\u2717 {msg}")
            success = False

    if success:
        return True, "Publication completed successfully!"
    return False, "Publication failed!"
