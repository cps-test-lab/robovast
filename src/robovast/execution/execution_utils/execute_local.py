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
import sys
from pprint import pprint

import yaml

from robovast.common import (get_execution_env_variables,
                             get_execution_variants, load_config)
from robovast.common.cli import get_project_config


def get_docker_image_from_yaml(yaml_path):
    """Parse the Kubernetes YAML template to extract the Docker image name."""
    try:
        with open(yaml_path, 'r') as f:
            yaml_content = yaml.safe_load(f)
        
        # Navigate to the container image specification
        containers = yaml_content['spec']['template']['spec']['containers']
        if containers:
            image = containers[0]['image']
            # Replace environment variable placeholder with actual value
            if '${ROS_DISTRO}' in image:
                ros_distro = os.environ.get('ROS_DISTRO', 'jazzy')
                image = image.replace('${ROS_DISTRO}', ros_distro)
            return image
    except Exception as e:
        print(f"Error parsing YAML file: {e}")
        return None
    return None

def initialize_local_execution(variant, debug=False):
    """Initialize common setup for local execution commands.
    
    Args:
        variant: The variant name to execute
        debug: Enable debug output
        
    Returns:
        Tuple of (config, output, docker_image, variant_configs)
        
    Raises:
        SystemExit: If initialization fails
    """
    # Get project configuration
    project_config = get_project_config()
    config = project_config.config_path
    output = project_config.results_dir
    
    execution_parameters = load_config(config, "execution")
    yaml_path = os.path.join(os.path.dirname(config), execution_parameters["kubernetes_manifest"])
    
    if not os.path.exists(yaml_path):
        print(f"Error: Kubernetes template not found: {yaml_path}", err=True)
        sys.exit(1)
    
    docker_image = get_docker_image_from_yaml(yaml_path)
    if not docker_image:
        print("Error: Could not extract Docker image from YAML file", err=True)
        sys.exit(1)
    
    print(f"Docker image: {docker_image}")
    print("-" * 60)

    variants = get_execution_variants(config)

    if variant not in variants:
        print(f"Error: variant '{variant}' not found in config.", err=True)
        print("Available variants:")
        for v in variants:
            print(f"  - {v}")
        sys.exit(1)

    variant_configs = {variant: variants[variant]}
    
    if debug:
        print("Variants:")
        pprint(variant_configs)
    
    return config, output, docker_image, variant_configs

def get_commandline(image, config_path, output_path, variant_name, run_num=0, shell=False):

    # Get the current user and group IDs to run docker with the same permissions
    uid = os.getuid()
    gid = os.getgid()

    docker_cmd = [
        'docker', 'run',
        '--rm',  # Remove container after execution
        '--user', f'{uid}:{gid}',  # Run as host user to avoid permission issues
        '-v', f'{os.path.abspath(config_path)}:/config',  # Bind mount temp_path to /config
        '-v', f'{os.path.abspath(output_path)}:/out',   # Bind mount output to /out
    ]

    env_vars = get_execution_env_variables(run_num, variant_name)
    for key, value in env_vars.items():
        docker_cmd.extend(['-e', f'{key}={value}'])

    if shell:
        # Interactive shell mode
        docker_cmd.extend(['-it', image, '/bin/bash'])
        print(f"Opening interactive shell in Docker container: {image}")
    else:
        # Normal execution mode
        docker_cmd.append(image)
        print(f"Executing Docker container: {image}")

    return docker_cmd

def execute_docker_container(image, config_path, temp_path, output_path, variant_name, run_num=0, shell=False):
    """Execute Docker container with the specified bind mounts."""
    docker_cmd = get_commandline(image, config_path, output_path, variant_name, run_num, shell)
    print(f"Docker command:\n{' '.join(docker_cmd)}")
    print("-" * 60)
    sys.stdout.flush()  # Ensure all output is flushed before starting docker
    
    try:
        if shell:
            # For interactive shell, use subprocess.call which properly inherits stdin/stdout/stderr
            # This gives proper TTY handling for interactive sessions
            return_code = subprocess.call(docker_cmd)
            return return_code
        else:
            # Normal execution mode - use subprocess.call instead of run to stream output in real-time
            # This ensures that stdout/stderr are directly inherited and shown immediately
            return_code = subprocess.call(docker_cmd)
            return return_code
    except subprocess.CalledProcessError as e:
        print(f"Error executing Docker container: return code {e.returncode}")
        return e.returncode
    except Exception as e:
        print(f"Unexpected error: {e}")
        return 1
