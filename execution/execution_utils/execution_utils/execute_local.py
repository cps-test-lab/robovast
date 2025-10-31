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

import argparse
import os
import subprocess
import sys
import tempfile
from pprint import pprint
import yaml
from robovast_common import (load_config, get_execution_env_variables,
                             get_execution_variants, prepare_run_configs)


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


def execute_docker_container(image, config_path, temp_path, output_path, variant_name, run_num=0, shell=False):
    """Execute Docker container with the specified bind mounts."""
    
    
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
    
    print(f"Docker command: {' '.join(docker_cmd)}")
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


def main():  # pylint: disable=too-many-return-statements

    parser = argparse.ArgumentParser(
        description='Execute scenario variant.',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    parser.add_argument('--config', type=str, required=True,
                        help='Path to .vast configuration file')
    parser.add_argument('--output', "-o", type=str, required=True,
                        help='Output directory of the execution')
    parser.add_argument('--variant', "-v", type=str, required=True,
                        help='Variant to execute')
    parser.add_argument('--debug', "-d", action='store_true',
                        help='Enable debug output')
    parser.add_argument('--shell', "-s", action='store_true',
                        help='Instead of running the scenario, login with shell')

    args = parser.parse_args()

    if not os.path.exists(args.config):
        print(f"Error: config file not found: {args.config}")
        return 1

    execution_parameters = load_config(args.config, "execution")
    # Get the Docker image from the Kubernetes YAML template
    yaml_path = os.path.join(os.path.dirname(args.config), execution_parameters["kubernetes_manifest"])
    
    if not os.path.exists(yaml_path):
        print(f"Error: Kubernetes template not found: {yaml_path}")
        return 1
    
    docker_image = get_docker_image_from_yaml(yaml_path)
    if not docker_image:
        print("Error: Could not extract Docker image from YAML file")
        return 1
    
    print(f"Docker image: {docker_image}")
    print("-" * 60)

    variants =  get_execution_variants(args.config)

    if args.variant not in variants:
        print(f"Error: variant {args.variant} not found in config {args.config}. Available:")
        for variant in variants:
            print(f"  - {variant}")
        return 1

    variant_configs = {args.variant: variants[args.variant]}
    # variant_configs = variants
    print(f"Executing variant {args.variant} from {args.config}...")
    print(f"Output directory: {args.output}")

    os.makedirs(args.output, exist_ok=True)

    if args.debug:
        print("Variants:")
        pprint(variant_configs)
    print("-" * 60)

    try:
        temp_path = tempfile.TemporaryDirectory(prefix="robovast_local_", delete=not args.debug)
        if args.debug:
            print("Temp path:", temp_path.name)
        prepare_run_configs(args.variant, variant_configs, temp_path.name)

        # Execute the Docker container with bind mounts

        config_path = os.path.join(temp_path.name, "config", args.variant, args.variant)
        print(config_path)
        return_code = execute_docker_container(docker_image, config_path, temp_path.name, args.output, args.variant, shell=args.shell)
        return return_code

    except Exception as e:
        print(f"Error: {e}")
        return 1


if __name__ == '__main__':
    sys.exit(main())
