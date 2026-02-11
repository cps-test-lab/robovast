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
import os
import shutil
from importlib.resources import files

import yaml

from .common import convert_dataclasses_to_dict, get_scenario_parameters


def get_run_id():
    return f"run-{datetime.datetime.now().strftime('%Y-%m-%d-%H%M%S')}"


def get_execution_env_variables(run_num, config_name):
    run_id = get_run_id()
    scenario_id = f"{config_name}-{run_num}"
    return {
        'RUN_ID': run_id,
        'RUN_NUM': str(run_num),
        'SCENARIO_ID': scenario_id,
        'SCENARIO_CONFIG': config_name,
        'ROS_LOG_DIR': '/out/logs',
    }


def prepare_run_configs(run_id, run_data, output_dir):
    # Create the out directory structure: /out/$RUN_ID/
    out_dir = os.path.join(output_dir, "out", run_id)
    os.makedirs(out_dir, exist_ok=True)
    
    # Copy entrypoint.sh to the out directory
    entrypoint_src = str(files('robovast.execution.data').joinpath('entrypoint.sh'))
    entrypoint_dst = os.path.join(out_dir, "entrypoint.sh")
    shutil.copy2(entrypoint_src, entrypoint_dst)

    # Copy vast file to the out directory
    shutil.copy2(run_data["vast"], out_dir)

    run_config_dir = os.path.join(out_dir, "_config")
    os.makedirs(run_config_dir, exist_ok=True)
    
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
        config = config_data.get('config')
        if config is not None:
            wrapped_config_data = {scenario_name: config}

            with open(os.path.join(out_dir, config_data.get("name"), 'scenario.config'), 'w') as f:
                converted_config_data = convert_dataclasses_to_dict(wrapped_config_data)
                yaml.dump(converted_config_data, f, default_flow_style=False, sort_keys=False)
