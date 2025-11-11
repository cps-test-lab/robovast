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

import yaml

from .common import convert_dataclasses_to_dict, get_scenario_parameters


def get_run_id():
    return f"run-{datetime.datetime.now().strftime('%Y-%m-%d-%H%M%S')}"


def get_execution_env_variables(run_num, variant_name):
    run_id = get_run_id()
    scenario_id = f"{variant_name}-{run_num}"
    return {
        'RUN_ID': run_id,
        'RUN_NUM': str(run_num),
        'SCENARIO_ID': scenario_id,
        'SCENARIO_CONFIG': variant_name,
        'ROS_LOG_DIR': '/out/logs',
    }


def prepare_run_configs(run_id, variants, output_dir):
    # Create the config directory structure: /config/$RUN_ID/
    config_dir = os.path.join(output_dir, "config", run_id)
    os.makedirs(config_dir, exist_ok=True)
    for variant_data in variants:
        scenario_dir = os.path.join(config_dir, variant_data.get("name"))
        os.makedirs(scenario_dir, exist_ok=True)

        # Copy scenario file
        original_scenario_path = variant_data.get('_scenario_file')
        shutil.copy2(original_scenario_path, os.path.join(scenario_dir, 'scenario.osc'))

        # Copy filtered files
        for config_file in variant_data.get("_scenario_files", []):
            src_path = os.path.join(os.path.dirname(original_scenario_path), config_file)
            dst_path = os.path.join(scenario_dir, config_file)
            os.makedirs(os.path.dirname(dst_path), exist_ok=True)
            shutil.copy2(src_path, dst_path)

        # Copy variant files
        if "_variant_files" in variant_data:
            for config_rel_path, config_path in variant_data["_variant_files"]:
                if not os.path.exists(config_path):
                    raise FileNotFoundError(f"Variant file {config_path} does not exist.")
                src_path = config_path
                dst_path = os.path.join(scenario_dir, config_rel_path)
                os.makedirs(os.path.dirname(dst_path), exist_ok=True)
                shutil.copy2(src_path, dst_path)

        # Create variant file if needed
        variant = variant_data.get('variant')
        if variant is not None:
            # Get the scenario name from the scenario file
            original_scenario_path = variant_data.get('_scenario_file')
            try:
                scenario_params = get_scenario_parameters(original_scenario_path)
                # get_scenario_parameters returns {scenario_name: [params]}
                scenario_name = next(iter(scenario_params.keys()))
                # Wrap variant_data under the scenario name
                wrapped_variant_data = {scenario_name: variant}
            except Exception as e:
                raise RuntimeError(f"Could not get scenario name from {original_scenario_path}: {e}") from e

            with open(os.path.join(scenario_dir, 'scenario.variant'), 'w') as f:
                converted_variant_data = convert_dataclasses_to_dict(wrapped_variant_data)
                yaml.dump(converted_variant_data, f, default_flow_style=False, sort_keys=False)
