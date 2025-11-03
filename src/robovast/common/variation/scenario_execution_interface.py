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

import os
import subprocess
import tempfile

import yaml

from robovast.common.file_cache import FileCache


def get_scenario_parameter_template(
    scenario_file: str,
    docker_image: str = "ghcr.io/cps-test-lab/robovast:latest"
) -> dict:
    config_dir = os.path.dirname(os.path.abspath(scenario_file))

    file_cache = FileCache()
    file_cache.set_current_data_directory(config_dir)
    hash_files = [scenario_file]
    cache_file_name = f"scenario_template_{os.path.basename(scenario_file)}.yaml"
    cached_file = file_cache.get_cached_file(hash_files, cache_file_name)
    if cached_file:
        try:
            return yaml.safe_load(cached_file)
        except yaml.YAMLError as e:
            print(f"Failed to parse cached template file: {e}")
            cached_file = None  # Force regeneration if parsing fails
    if cached_file is None:
        template_dict = None
        # Create temporary directory for output
        with tempfile.TemporaryDirectory(prefix="scenario_template_") as temp_dir:
            output_dir = os.path.join(temp_dir, "out")
            os.makedirs(output_dir, exist_ok=True)

            template_file = os.path.join(output_dir, "template.yaml")

            # Get current user and group IDs to run docker with same permissions
            uid = os.getuid()
            gid = os.getgid()

            # Build Docker command
            docker_cmd = [
                'docker', 'run',
                '--rm',  # Remove container after execution
                '--user', f'{uid}:{gid}',  # Run as host user to avoid permission issues
                '-v', f'{config_dir}:/config',  # Bind mount config directory
                '-v', f'{output_dir}:/out',  # Bind mount output directory
                docker_image,
                'ros2', 'run', 
                'scenario_execution', 'scenario_execution',
                '--scenario-parameter-file', '/out/template.yaml',
                '--create-scenario-parameter-file-template',
                os.path.join('/config', os.path.basename(scenario_file))
            ]

            print(f"Generating scenario parameter template...")
            print(f"Command: {' '.join(docker_cmd)}")

            try:
                subprocess.run(
                    docker_cmd,
                    capture_output=True,
                    text=True,
                    check=True
                )

            except subprocess.CalledProcessError as e:
                print(f"Error executing Docker container:")
                print(f"Return code: {e.returncode}")
                if e.stdout:
                    print(f"Stdout: {e.stdout}")
                if e.stderr:
                    print(f"Stderr: {e.stderr}")
                raise

            # Read and parse the generated template file
            if not os.path.exists(template_file):
                raise RuntimeError(
                    f"Template file was not generated at expected location: {template_file}"
                )

            try:
                with open(template_file, 'r') as f:
                    template_dict = yaml.safe_load(f)
            except yaml.YAMLError as e:
                raise yaml.YAMLError(f"Failed to parse generated template file: {e}")

            if template_dict is None:
                raise RuntimeError("Generated template file is empty")

        print(f"Successfully generated scenario parameter template")
        file_cache.save_file_to_cache(hash_files, cache_file_name, file_content=yaml.dump(template_dict))

        return template_dict
    return {}
