#!/usr/bin/env python3
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

"""Setup utilities for cluster execution."""

import logging
import os
from importlib.metadata import entry_points

import yaml

from robovast.common.cli.project_config import ProjectConfig

logger = logging.getLogger(__name__)

# Flag file name to store the cluster config name that was used for setup
CLUSTER_CONFIG_FLAG_FILE = ".robovast_cluster_config"


def get_cluster_config_flag_path():
    """Get the path to the cluster config flag file.

    The flag file is stored in the same directory as the project file.

    Returns:
        str: Path to the cluster config flag file

    Raises:
        RuntimeError: If no project file is found
    """
    project_file = ProjectConfig.find_project_file()
    if not project_file:
        raise RuntimeError(
            "Project not initialized. Run 'vast init <config-file>' first."
        )

    project_dir = os.path.dirname(project_file)
    return os.path.join(project_dir, CLUSTER_CONFIG_FLAG_FILE)


def save_cluster_setup_info(config_name, setup_kwargs):
    """Save the cluster setup info to a flag file.

    Args:
        config_name (str): Name of the cluster config plugin used for setup
        setup_kwargs (dict): Arguments passed to the setup function
    """
    flag_path = get_cluster_config_flag_path()
    data = {
        "name": config_name,
        "kwargs": setup_kwargs
    }
    with open(flag_path, 'w') as f:
        yaml.dump(data, f, default_flow_style=False)


def load_cluster_setup_info():
    """Load the cluster setup info from the flag file.

    Returns:
        tuple: (config_name, setup_kwargs)
    """
    try:
        flag_path = get_cluster_config_flag_path()
        if os.path.exists(flag_path):
            with open(flag_path, 'r') as f:
                content = f.read().strip()
                try:
                    data = yaml.safe_load(content)
                    if isinstance(data, dict):
                        return data.get("name"), data.get("kwargs", {})
                    # Backward compatibility handling (if it's just the name string)
                    return str(data) if data else None, {}
                except yaml.YAMLError:
                    # Backward compatibility fallback
                    return content, {}
    except RuntimeError:
        # Project not initialized
        pass
    return None, {}


def load_cluster_config_name():
    """Load the cluster config name from the flag file.

    Returns:
        str: Name of the cluster config plugin, or None if file doesn't exist
    """
    name, _ = load_cluster_setup_info()
    return name


def get_cluster_namespace():
    """Load the cluster namespace from the setup flag file.

    Returns:
        str: Kubernetes namespace for cluster execution, or "default" if not set
    """
    _, kwargs = load_cluster_setup_info()
    return kwargs.get("namespace", "default")


def delete_cluster_config_flag():
    """Delete the cluster config flag file."""
    try:
        flag_path = get_cluster_config_flag_path()
        if os.path.exists(flag_path):
            os.remove(flag_path)
    except RuntimeError:
        # Project not initialized, nothing to delete
        pass


def load_cluster_config_plugins():
    """Load all available cluster config plugins from entry points.

    Returns:
        dict: Dictionary mapping plugin names to their class objects
    """
    plugins = {}
    try:
        eps = entry_points(group='robovast.cluster_configs')
        for ep in eps:
            try:
                plugin_class = ep.load()
                plugins[ep.name] = plugin_class
            except Exception as e:
                logger.warning(f"Failed to load cluster config plugin '{ep.name}': {e}")
    except Exception as e:
        logger.warning(f"Failed to load cluster config plugins: {e}")

    return plugins


def get_cluster_config(config_name):
    """Get a cluster configuration instance by name.

    Args:
        config_name: Name of the cluster config plugin to use.

    Returns:
        BaseConfig: Instance of the selected cluster configuration class

    Raises:
        ValueError: If config_name is not found in available plugins
    """
    if config_name is None:
        return None

    plugins = load_cluster_config_plugins()

    if config_name not in plugins:
        available = ", ".join(plugins.keys()) if plugins else "none"
        raise ValueError(
            f"Cluster config '{config_name}' not found. "
            f"Available configs: {available}"
        )

    # Instantiate and return the config class
    return plugins[config_name]()


def setup_server(config_name=None, list_configs=False, force=False, **cluster_kwargs):
    """Set up transfer mechanism for cluster execution.

    Args:
        config_name (str, optional): Name of the cluster config plugin to use
        list_configs (bool): If True, list available configs and exit
        **cluster_kwargs: Cluster-specific options to pass to setup_cluster()

    Returns:
        None

    Raises:
        RuntimeError: If cluster is already set up
    """
    if list_configs:
        plugins = load_cluster_config_plugins()
        if plugins:
            logger.info("Available cluster configurations:")
            for name in sorted(plugins.keys()):
                logger.info(f"  - {name}")
        else:
            logger.info("No cluster configurations available.")
        return

    if config_name is None:
        raise ValueError(
            "No cluster config specified. Use --config <name> to select a config, "
            "or --list to see available configs."
        )

    # Check if cluster is already set up
    existing_config = load_cluster_config_name()
    if existing_config and not force:
        raise RuntimeError(
            f"Cluster is already set up with '{existing_config}' config.\n"
            f"Run 'vast execution cluster cleanup' first to clean up the existing setup."
        )

    cluster_config = get_cluster_config(config_name)
    cluster_config.setup_cluster(**cluster_kwargs)

    # Save the config name and kwargs to flag file after successful setup
    flag_path = get_cluster_config_flag_path()
    save_cluster_setup_info(config_name, cluster_kwargs)
    logger.debug(f"Cluster config '{config_name}' saved to {flag_path}")


def delete_server(config_name=None, **cluster_kwargs_override):
    """Clean up transfer mechanism for cluster execution.

    Args:
        config_name (str, optional): Name of the cluster config plugin to use.
                                     If not provided, will auto-detect from flag file.
        **cluster_kwargs_override: Optional kwargs to pass to cleanup_cluster (e.g. namespace).
                                   When auto-detecting, these override stored kwargs.
                                   When config_name is given, these are the only kwargs used.

    Returns:
        None
    """
    cluster_kwargs = {}

    # Auto-detect config from flag file if not provided
    if config_name is None:
        name, stored_kwargs = load_cluster_setup_info()
        config_name = name

        # Use stored kwargs for cleanup; CLI overrides take precedence
        if stored_kwargs:
            cluster_kwargs = dict(stored_kwargs)
        if cluster_kwargs_override:
            cluster_kwargs.update(cluster_kwargs_override)

        if config_name:
            logger.debug(f"Auto-detected cluster config: {config_name}")
        else:
            raise ValueError(
                "No cluster config specified and no saved config found. "
                "Use --cluster-config <name> to select a config, or run setup first."
            )
    else:
        # Explicit config: use only CLI-provided kwargs (e.g. -n namespace)
        cluster_kwargs = dict(cluster_kwargs_override)

    cluster_config = get_cluster_config(config_name)
    cluster_config.cleanup_cluster(**cluster_kwargs)

    # Delete the flag file after successful cleanup
    delete_cluster_config_flag()
