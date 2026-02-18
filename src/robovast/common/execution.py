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

import datetime
import logging
import os
import shutil
from importlib.resources import files
from pprint import pformat

import yaml

from .common import convert_dataclasses_to_dict, get_scenario_parameters

logger = logging.getLogger(__name__)


def get_run_id():
    return f"run-{datetime.datetime.now().strftime('%Y-%m-%d-%H%M%S')}"


def get_execution_env_variables(run_num, config_name, additional_env=None):
    """Get environment variables for execution.

    Args:
        run_num: Run number
        config_name: Configuration name
        additional_env: Optional list of additional environment variables in format:
                       [{"KEY": "value"}]

    Returns:
        Dictionary of environment variables
    """
    run_id = get_run_id()
    test_id = f"{config_name}-{run_num}"
    env_vars = {
        'TEST_ID': test_id,
        'ROS_LOG_DIR': '/out/logs',
    }

    # Add custom environment variables from execution config
    if additional_env and isinstance(additional_env, list):
        for env_item in additional_env:
            if isinstance(env_item, dict):
                # Handle simple format: {"KEY": "value"}
                for key, value in env_item.items():
                    env_vars[key] = value

    return env_vars


def prepare_run_configs(out_dir, run_data):
    # Create the output directory structure
    logger.debug(f"Run Configs: {pformat(run_data)}")
    os.makedirs(out_dir, exist_ok=True)

    # Copy entrypoint.sh to the out directory
    entrypoint_src = str(files('robovast.execution.data').joinpath('entrypoint.sh'))
    entrypoint_dst = os.path.join(out_dir, "entrypoint.sh")
    shutil.copy2(entrypoint_src, entrypoint_dst)

    # Copy vast file to the out directory
    shutil.copy2(run_data["vast"], out_dir)

    run_config_dir = os.path.join(out_dir, "_config")
    os.makedirs(run_config_dir, exist_ok=True)

    # Save scenario variations as YAML in _config subdirectory
    scenario_variations_path = os.path.join(run_config_dir, "configurations.yaml")
    with open(scenario_variations_path, 'w') as f:
        yaml.dump(convert_dataclasses_to_dict(run_data), f, default_flow_style=False, sort_keys=False)
    logger.debug(f"Saved configurations to {scenario_variations_path}")

    vast_file_path = os.path.dirname(run_data["vast"])
    # copy scenario_file
    scenario_file_path = os.path.join(vast_file_path, run_data["scenario_file"])
    shutil.copy2(scenario_file_path, out_dir)

    # Copy test files
    for config_file in run_data.get("_test_files", []):
        src_path = os.path.join(vast_file_path, config_file)
        dst_path = os.path.join(run_config_dir, config_file)
        os.makedirs(os.path.dirname(dst_path), exist_ok=True)
        shutil.copy2(src_path, dst_path)

    # get scenario name
    original_scenario_path = os.path.join(vast_file_path, run_data.get("scenario_file"))
    try:
        scenario_params = get_scenario_parameters(original_scenario_path)
        scenario_name = next(iter(scenario_params.keys()))

        if scenario_name is None:
            raise ValueError(f"Scenario name not found in {original_scenario_path}")
    except Exception as e:
        raise RuntimeError(f"Could not get scenario name from {original_scenario_path}: {e}") from e

    for config_data in run_data["configs"]:
        test_config_dir = os.path.join(out_dir, config_data.get("name"), "_config")

        # Copy config files
        if "_config_files" in config_data:
            for config_rel_path, config_path in config_data["_config_files"]:
                if not os.path.exists(config_path):
                    raise FileNotFoundError(f"Config file {config_path} does not exist.")
                src_path = config_path
                dst_path = os.path.join(test_config_dir, config_rel_path)
                os.makedirs(os.path.dirname(dst_path), exist_ok=True)
                shutil.copy2(src_path, dst_path)

        # Create config file if needed
        if "config" in config_data:
            config = config_data.get('config')
            if config is not None:
                wrapped_config_data = {scenario_name: config}
                dst_path = os.path.join(out_dir, config_data.get("name"), 'scenario.config')
                os.makedirs(os.path.dirname(dst_path), exist_ok=True)
                with open(dst_path, 'w') as f:
                    converted_config_data = convert_dataclasses_to_dict(wrapped_config_data)
                    yaml.dump(converted_config_data, f, default_flow_style=False, sort_keys=False)


def generate_execution_yaml_script(runs, execution_params=None, output_dir_var="${RESULTS_DIR}"):
    """Generate shell script code to create execution.yaml with ISO formatted timestamp.

    Args:
        runs: Number of runs
        execution_params: Dictionary containing execution parameters (run_as_user, env, etc.)
        output_dir_var: Shell variable name for the output directory (default: ${RESULTS_DIR})

    Returns:
        String containing shell script code to create execution.yaml
    """
    if execution_params is None:
        execution_params = {}

    script = f'echo "Creating execution.yaml..."\n'
    script += f'EXECUTION_TIME=$(date -u +"%Y-%m-%dT%H:%M:%SZ")\n'
    script += f'cat > "{output_dir_var}/execution.yaml" << EOF\n'
    script += 'execution_time: ${EXECUTION_TIME}\n'
    script += f'runs: {runs}\n'
    script += f'execution_type: local\n'
    script += f'image: {execution_params.get("image")}\n'

    # Add run_as_user if provided
    run_as_user = execution_params.get('run_as_user')
    if run_as_user is not None:
        script += f'run_as_user: {run_as_user}\n'

    # Add env if provided
    env = execution_params.get('env')
    if env:
        script += 'env:\n'
        for env_item in env:
            if isinstance(env_item, dict):
                for key, value in env_item.items():
                    # Escape special characters for heredoc
                    escaped_value = str(value).replace('"', '\\"').replace('$', '\\$') if value is not None else ""
                    script += f'  {key}: "{escaped_value}"\n'

    script += 'EOF\n'
    script += f'echo ""\n\n'
    return script


def create_execution_yaml(runs, output_dir, execution_params=None):
    """Create execution.yaml file with ISO formatted timestamp.

    Args:
        runs: Number of runs to include in execution.yaml
        output_dir: Directory where execution.yaml will be created
        execution_params: Dictionary containing execution parameters (run_as_user, env, etc.)
    """
    if execution_params is None:
        execution_params = {}

    execution_yaml_path = os.path.join(output_dir, "execution.yaml")
    execution_time = datetime.datetime.now(datetime.timezone.utc).isoformat() + 'Z'

    execution_data = {
        'execution_time': execution_time,
        'runs': runs,
        'execution_type': 'cluster',
        'image': execution_params.get('image')
    }

    # Add run_as_user if provided
    run_as_user = execution_params.get('run_as_user')
    if run_as_user is not None:
        execution_data['run_as_user'] = run_as_user

    # Add env if provided
    env = execution_params.get('env')
    if env:
        # Convert list of dicts to a single dict for cleaner YAML output
        env_dict = {}
        for env_item in env:
            if isinstance(env_item, dict):
                env_dict.update(env_item)
        if env_dict:
            execution_data['env'] = env_dict

    with open(execution_yaml_path, 'w') as f:
        yaml.dump(execution_data, f, default_flow_style=False, sort_keys=False)

    logger.debug(f"Created execution.yaml with timestamp: {execution_time}")
