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
import sys
import tempfile

from robovast.common import (get_execution_env_variables, load_config,
                             prepare_run_configs)
from robovast.common.cli import get_project_config
from robovast.common.variant_generation import generate_scenario_variations


def initialize_local_execution(variant, output_dir, runs, debug=False, feedback_callback=print):
    """Initialize common setup for local execution commands.

    Performs all common setup steps including:
    - Loading project and execution configuration
    - Validating variant exists
    - Creating output directory
    - Preparing run configuration files
    - Generating config path

    Args:
        variant: The variant name to execute
        output_dir: Directory where output files will be written, if none a temporary directory is created
        runs: Number of runs per variant
        debug: Enable debug output
        feedback_callback: Function to call for feedback messages (e.g., print or click.echo)

    Raises:
        SystemExit: If initialization fails
    """
    # Load configuration
    project_config = get_project_config()
    config_path = project_config.config_path
    execution_parameters = load_config(config_path, "execution")
    docker_image = execution_parameters.get("image", "ghcr.io/cps-test-lab/robovast:latest")
    results_dir = project_config.results_dir

    # Use execution_parameters value if runs is not provided
    if runs is None:
        if "runs" not in execution_parameters:
            feedback_callback("Error: Number of runs not specified in command or config.")
            sys.exit(1)
        else:
            runs = execution_parameters["runs"]

    feedback_callback(f"Docker image: {docker_image}")
    feedback_callback("-" * 60)

    # Generate and filter variants
    temp_dir = tempfile.TemporaryDirectory(prefix="robovast_execution_")
    variants, _ = generate_scenario_variations(
        variation_file=config_path,
        progress_update_callback=print,
        output_dir=temp_dir.name
    )

    if not variants:
        feedback_callback("Error: No variants found in config.", file=sys.stderr)
        sys.exit(1)

    # Filter to specific variant if requested
    if variant:
        found_variant = None
        for v in variants:
            if v['name'] == variant:
                found_variant = v
                break

        if not found_variant:
            feedback_callback(f"Error: Variant '{variant}' not found in config.", file=sys.stderr)
            feedback_callback("Available variants:")
            for v in variants:
                feedback_callback(f"  - {v['name']}")
            sys.exit(1)

        variants = [found_variant]

    feedback_callback(f"Preparing {len(variants)} variants from {config_path}...")
    feedback_callback(f"Output directory: {output_dir}")

    # Create the output directory
    feedback_callback("-" * 60)

    # Create temp directory for run() or use output_dir for prepare_run()
    temp_path = None
    if not output_dir:
        temp_path = tempfile.TemporaryDirectory(prefix="robovast_local_", delete=False)
        feedback_callback(f"Using temporary directory for config files: {temp_path.name}")
        if debug:
            feedback_callback(f"Temp path: {temp_path.name}")
        config_dir = temp_path.name
    else:
        try:
            os.makedirs(output_dir, exist_ok=True)
        except Exception as e:  # pylint: disable=broad-except
            feedback_callback(f"Error creating output directory: {e}", file=sys.stderr)
            sys.exit(1)
        config_dir = output_dir

    try:
        prepare_run_configs("local", variants, config_dir)
        config_path_result = os.path.join(config_dir, "config", "local")
        feedback_callback(f"Config path: {config_path_result}")
    except Exception as e:  # pylint: disable=broad-except
        feedback_callback(f"Error preparing run configs: {e}", file=sys.stderr)
        sys.exit(1)

    feedback_callback(f"Configuration files prepared in: {config_dir}")
    feedback_callback("-" * 60)

    docker_configs = []
    for run_number in range(runs):
        for variant_entry in variants:
            docker_configs.append((docker_image, os.path.abspath(os.path.join(
                config_path_result, variant_entry["name"])), variant_entry['name'], run_number))

    generate_docker_run_script(docker_configs, results_dir, os.path.join(config_dir, "run.sh"))
    return os.path.join(config_dir, "run.sh")


def get_commandline(image, config_path, output_path, variant_name, run_num=0, shell=False):

    # Get the current user and group IDs to run docker with the same permissions
    uid = os.getuid()
    gid = os.getgid()

    docker_cmd = [
        'docker', 'run',
        '--rm',  # Remove container after execution
        '--user', f'{uid}:{gid}',  # Run as host user to avoid permission issues
        '-v', f'{config_path}:/config',  # Bind mount temp_path to /config
        '-v', f'{output_path}:/out',   # Bind mount output to /out
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


DOCKER_RUN_TEMPLATE = """#!/usr/bin/env bash

# Default Docker image
DOCKER_IMAGE="ghcr.io/cps-test-lab/robovast:latest"
NETWORK_MODE=""
USE_GUI=true
USE_SHELL=false
CONTAINER_NAME="robovast"
RUN_ID="run-$(date +%Y-%m-%d-%H%M%S)"
RESULTS_DIR=

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
    --output DIR        Override the results output directory
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
        --output)
            if [[ "$2" != /* ]]; then
                echo "Error: --output must be an absolute path (starting with /)"
                exit 1
            fi
            echo "Overriding results directory to: $2"
            RESULTS_DIR="$2"
            shift 2
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

mkdir -p ${RESULTS_DIR}
"""


def generate_docker_run_script(variant_configs, results_dir, output_script_path):
    """Generate a shell script to run Docker containers sequentially.

    Args:
        variant_configs: List of tuples (image, config_path, variant_name, run_num)
        output_script_path: Path where the script should be written
    """
    if not variant_configs:
        raise ValueError("At least one variant configuration is required")

    # Use the first variant's image as the default
    default_image = variant_configs[0][0]

    # Start with the template, replacing the image
    # Replace only the first occurrence of DOCKER_IMAGE and RESULTS_DIR
    script = DOCKER_RUN_TEMPLATE
    script = script.replace(
        'DOCKER_IMAGE="ghcr.io/cps-test-lab/robovast:latest"',
        f'DOCKER_IMAGE="{default_image}"', 1
    )
    script = script.replace(
        'RESULTS_DIR=',
        f'RESULTS_DIR="{results_dir}/${{RUN_ID}}"', 1
    )

    total_variants = len(variant_configs)

    # Generate docker run commands for each variant
    for idx, (image, config_path, variant_name, run_num) in enumerate(variant_configs, 1):
        test_path = os.path.join("${RESULTS_DIR}", variant_name, str(run_num))
        cmd_line = get_commandline(image, config_path, test_path, variant_name, run_num)

        # Add progress message
        script += f'\necho ""\n'
        script += f'echo "{"=" * 60}"\n'
        script += f'echo "{idx}/{total_variants} Executing variant {variant_name}, run {run_num}"\n'
        script += f'echo "{"=" * 60}"\n'
        script += f'echo ""\n\n'
        script += f'mkdir -p {test_path}/logs\n'

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

        # Add docker run command for this variant
        script += 'docker run $INTERACTIVE \\\n'
        script += f'    --name "$CONTAINER_NAME" \\\n'
        script += '    $NETWORK_MODE \\\n'
        script += '    $GUI_OPTIONS \\\n'
        script += "\n".join(docker_params)
        script += f'\n    "$DOCKER_IMAGE" \\\n    $COMMAND\n\n'

        # Check exit code after each run
        if idx < total_variants:
            # Not the last one - check and continue
            script += '# Check exit code\n'
            script += 'EXIT_CODE=$?\n'
            script += 'if [ $EXIT_CODE -ne 0 ]; then\n'
            script += f'    echo "Error: Variant {idx}/{total_variants} ({variant_name}) failed with exit code $EXIT_CODE"\n'
            script += '    cleanup\n'
            script += '    exit $EXIT_CODE\n'
            script += 'fi\n\n'
        else:
            # Last one - capture exit code and cleanup
            script += '# Capture exit code and cleanup\n'
            script += 'EXIT_CODE=$?\n'
            script += 'if [ $EXIT_CODE -eq 0 ]; then\n'
            script += f'    echo ""\n'
            script += f'    echo "{"=" * 60}"\n'
            script += f'    echo "All {total_variants} variant(s) completed successfully!"\n'
            script += f'    echo "{"=" * 60}"\n'
            script += 'else\n'
            script += f'    echo "Error: Variant {idx}/{total_variants} ({variant_name}) failed with exit code $EXIT_CODE"\n'
            script += 'fi\n'
            script += 'cleanup\n'
            script += 'exit $EXIT_CODE\n'

    try:
        with open(output_script_path, 'w') as f:
            f.write(script)
        os.chmod(output_script_path, 0o755)  # Make the script executable
    except Exception as e:  # pylint: disable=broad-except
        print(f"Error writing Docker run script: {e}")
