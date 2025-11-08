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
import tempfile
from pprint import pprint

from robovast.common import (get_execution_env_variables,
                             get_execution_variants, load_config,
                             prepare_run_configs)
from robovast.common.cli import get_project_config


def initialize_local_execution(variant, output_dir, debug=False, use_temp_dir=False):
    """Initialize common setup for local execution commands.

    Performs all common setup steps including:
    - Loading project and execution configuration
    - Validating variant exists
    - Creating output directory
    - Preparing run configuration files
    - Generating config path

    Args:
        variant: The variant name to execute
        output_dir: Directory where output files will be written
        debug: Enable debug output
        use_temp_dir: If True, creates a temporary directory for config files (used by run())

    Returns:
        Tuple of (docker_image, config_path, temp_path)
        where temp_path is a TemporaryDirectory object (or None if use_temp_dir=False)

    Raises:
        SystemExit: If initialization fails
    """
    # Get project configuration
    project_config = get_project_config()
    config = project_config.config_path

    execution_parameters = load_config(config, "execution")
    docker_image = execution_parameters["image"]
    print(f"Docker image: {docker_image}")
    print("-" * 60)

    variants, variant_files_dir = get_execution_variants(config)

    if variant not in variants:
        print(f"Error: variant '{variant}' not found in config.")
        print("Available variants:")
        for v in variants:
            print(f"  - {v}")
        sys.exit(1)

    variant_configs = {variant: variants[variant]}

    if debug:
        print("Variants:")
        pprint(variant_configs)

    print(f"Executing variant '{variant}' from {config}...")
    print(f"Output directory: {output_dir}")

    # Create the output directory
    try:
        os.makedirs(output_dir, exist_ok=True)
    except Exception as e:  # pylint: disable=broad-except
        print(f"Error creating output directory: {e}", file=sys.stderr)
        sys.exit(1)
    print("-" * 60)

    # Create temp directory for run() or use output_dir for prepare_run()
    temp_path = None
    if use_temp_dir:
        temp_path = tempfile.TemporaryDirectory(prefix="robovast_local_", delete=not debug)
        if debug:
            print(f"Temp path: {temp_path.name}")
        config_dir = temp_path.name
    else:
        config_dir = output_dir

    try:
        prepare_run_configs(variant, variant_configs, variant_files_dir.name, config_dir)
        config_path = os.path.join(config_dir, "config", variant, variant)
        print(f"Config path: {config_path}")
    except Exception as e:  # pylint: disable=broad-except
        print(f"Error preparing run configs: {e}", file=sys.stderr)
        sys.exit(1)

    return docker_image, config_path, temp_path


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


DOCKER_RUN_TEMPLATE = """#!/usr/bin/env bash

# Default Docker image
DOCKER_IMAGE="ghcr.io/cps-test-lab/robovast:latest"
NETWORK_MODE=""
USE_GUI=true
USE_SHELL=false
CONTAINER_NAME="robovast"

# Variable to track if cleanup has run
CLEANUP_DONE=0

# Cleanup function
cleanup() {
    if [ $CLEANUP_DONE -eq 1 ]; then
        return
    fi
    CLEANUP_DONE=1
    
    echo ""
    echo "Cleaning up container..."
    # Kill the container with timeout
    timeout 3 docker kill "$CONTAINER_NAME" 2>/dev/null || true
    # Force remove the container with timeout
    timeout 3 docker rm -f "$CONTAINER_NAME" 2>/dev/null || true
}

# Set up signal handlers
trap 'cleanup; exit 130' SIGINT SIGTERM

# Show help
show_help() {
    cat << EOF
Usage: $(basename "$0") [OPTIONS] [COMMAND]

Run the robovast Docker container.

OPTIONS:
    --image IMAGE       Use a custom Docker image (default: ghcr.io/cps-test-lab/robovast:latest)
    --network-host      Use host networking mode
    --no-gui            Disable host GUI support
    --shell             Launch an interactive shell instead of running the test
    -h, --help          Show this help message
EOF
}

# Parse command-line arguments
while [ $# -gt 0 ]; do
    case $1 in
        -h|--help)
            show_help
            exit 0
            ;;
        --image)
            DOCKER_IMAGE="$2"
            shift 2
            ;;
        --network-host)
            NETWORK_MODE="--network host"
            shift
            ;;
        --no-gui)
            USE_GUI=false
            shift
            ;;
        --shell)
            USE_SHELL=true
            shift
            ;;
        *)
            break
            ;;
    esac
done

# GUI setup
GUI_OPTIONS=""
if [ "$USE_GUI" = true ]; then
    # Allow Docker to access the X server
    xhost +local:docker > /dev/null 2>&1
    GUI_OPTIONS="--env DISPLAY=$DISPLAY --volume /tmp/.X11-unix:/tmp/.X11-unix:rw --device /dev/dri:/dev/dri --group-add video"
fi

# Determine command to run and interactive mode
if [ "$USE_SHELL" = true ]; then
    COMMAND="/bin/bash"
    INTERACTIVE="-it"
    echo "--------------------------------------------------------"
    echo "Execute the following command to run the test:"
    echo
    echo "ros2 run scenario_execution_ros scenario_execution_ros -o /out /config/scenario.osc --scenario-parameter-file /config/scenario.variant"
    echo "--------------------------------------------------------"
else
    COMMAND="$*"
    INTERACTIVE=""
fi

docker run $INTERACTIVE \
    --name "$CONTAINER_NAME" \
    $NETWORK_MODE \
    $GUI_OPTIONS \
"""


def generate_docker_run_script(image, config_path, output_path, variant_name, run_num, output_script_path):
    """Generate a shell script to run the Docker container with the correct parameters."""
    cmd_line = get_commandline(image, config_path, output_path, variant_name, run_num)

    # Start with the template, replacing the image
    script = DOCKER_RUN_TEMPLATE.replace(
        'DOCKER_IMAGE="ghcr.io/cps-test-lab/robovast:latest"',
        f'DOCKER_IMAGE="{image}"'
    )

    # Extract docker run parameters from cmd_line (skip 'docker', 'run')
    docker_params = []
    i = 2  # Skip 'docker' and 'run'
    while i < len(cmd_line):
        arg = cmd_line[i]
        if arg == image:
            # Stop when we reach the image name
            break
        elif arg in ['-v', '-e', '--user']:
            # Options with values
            docker_params.append(f"    {arg} {cmd_line[i+1]} \\")
            i += 2
        else:
            # Options without values
            docker_params.append(f"    {arg} \\")
            i += 1

    # Add the parameters to the script
    script += "\n".join(docker_params)
    script += f'\n    "$DOCKER_IMAGE" \\\n    $COMMAND\n\n'
    script += '# Capture exit code and cleanup\n'
    script += 'EXIT_CODE=$?\n'
    script += 'cleanup\n'
    script += 'exit $EXIT_CODE\n'

    try:
        with open(output_script_path, 'w') as f:
            f.write(script)
        os.chmod(output_script_path, 0o755)  # Make the script executable
    except Exception as e:  # pylint: disable=broad-except
        print(f"Error writing Docker run script: {e}")
