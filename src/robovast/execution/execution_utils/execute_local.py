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

import logging
import os
import sys
import tempfile
from importlib.resources import files

from robovast.common import (get_execution_env_variables, load_config,
                             prepare_run_configs)
from robovast.common.cli import get_project_config
from robovast.common.config_generation import generate_scenario_variations

logger = logging.getLogger(__name__)


def initialize_local_execution(config, output_dir, runs, feedback_callback=logging.debug):
    """Initialize common setup for local execution commands.

    Performs all common setup steps including:
    - Loading project and execution configuration
    - Validating config exists
    - Creating output directory
    - Preparing run configuration files
    - Generating config path

    Args:
        config: The config name to execute
        output_dir: Directory where output files will be written, if none a temporary directory is created
        runs: Number of runs per config
        feedback_callback: Function to call for feedback messages (e.g., print or click.echo)

    Raises:
        SystemExit: If initialization fails
    """
    if output_dir:
        logger.info(f"Initializing local execution environment in '{output_dir}'...")
    else:
        logger.info("Initializing local execution environment in temporary directory...")
    # Load configuration
    project_config = get_project_config()
    config_path = project_config.config_path
    logger.debug(f"Loading config from: {config_path}")
    execution_parameters = load_config(config_path, "execution")
    docker_image = execution_parameters.get("image", "ghcr.io/cps-test-lab/robovast:latest")
    pre_command = execution_parameters.get("pre_command")
    post_command = execution_parameters.get("post_command")
    results_dir = project_config.results_dir
    run_as_user = execution_parameters.get("run_as_user")

    # Use execution_parameters value if runs is not provided
    if runs is None:
        if "runs" not in execution_parameters:
            logger.error("Number of runs not specified in command or config")
            feedback_callback("Error: Number of runs not specified in command or config.")
            sys.exit(1)
        else:
            runs = execution_parameters["runs"]

    logger.debug(f"Using Docker image: {docker_image}")

    # Check if run_as_user differs from local user
    host_uid = os.getuid()
    if run_as_user is not None and run_as_user != host_uid:
        logger.info(f"Note: config specifies run_as_user={run_as_user}, but local execution will use host user UID={host_uid} to ensure proper file permissions on bind mounts")

    # Generate and filter configs
    logger.debug("Generating scenario variations")
    temp_dir = tempfile.TemporaryDirectory(prefix="robovast_execution_")
    run_data, _ = generate_scenario_variations(
        variation_file=config_path,
        progress_update_callback=None,
        output_dir=temp_dir.name
    )

    if not run_data["configs"]:
        logger.error("No configs found in vast-file")
        feedback_callback("Error: No configs found in vast-file.", file=sys.stderr)
        sys.exit(1)

    # Filter to specific config if requested
    if config:
        found_config = None
        for cfg in run_data["configs"]:
            if cfg['name'] == config:
                found_config = cfg
                break

        if not found_config:
            feedback_callback(f"Error: Config '{config}' not found in config.", file=sys.stderr)
            feedback_callback("Available configs:")
            for cfg in run_data["configs"]:
                feedback_callback(f"  - {cfg['name']}")
            sys.exit(1)

        run_data["configs"] = [found_config]

    logger.debug(f"Preparing {len(run_data['configs'])} configs from {config_path}...")
    logger.debug(f"Output directory: {output_dir}")

    # Create temp directory for run() or use output_dir for prepare_run()
    temp_path = None
    if not output_dir:
        temp_path = tempfile.TemporaryDirectory(prefix="robovast_local_", delete=False)
        logger.debug(f"Using temporary directory for config files: {temp_path.name}")
        logger.debug(f"Temp path: {temp_path.name}")
        config_dir = temp_path.name
    else:
        try:
            os.makedirs(output_dir, exist_ok=True)
        except Exception as e:  # pylint: disable=broad-except
            feedback_callback(f"Error creating output directory: {e}", file=sys.stderr)
            sys.exit(1)
        config_dir = output_dir

    try:
        prepare_run_configs("config", run_data, config_dir)
        config_path_result = os.path.join(config_dir, "out_template", "config")
        logger.debug(f"Config path: {config_path_result}")
    except Exception as e:  # pylint: disable=broad-except
        feedback_callback(f"Error preparing run configs: {e}", file=sys.stderr)
        sys.exit(1)

    logger.debug(f"Configuration files prepared in: {config_dir}")

    generate_docker_run_script(runs, run_data, config_path_result, pre_command, post_command, docker_image, results_dir, os.path.join(config_dir, "run.sh"))
    return os.path.join(config_dir, "run.sh")


def get_commandline(image, config_path, output_path, config_name, config_files, run_files, run_num=0, shell=False, pre_command=None, post_command=None):

    # Get the current user and group IDs to run docker with the same permissions
    uid = os.getuid()
    gid = os.getgid()

    # Get the path to the entrypoint.sh file from package data
    entrypoint_path = str(files('robovast.execution.data').joinpath('entrypoint.sh'))

    docker_cmd = [
        'docker', 'run',
        '--rm',  # Remove container after execution
        '--user', f'{uid}:{gid}',  # Run as host user to avoid permission issues
        '-v', f'{output_path}:/out',   # Bind mount output to /out
        '-v', f'{entrypoint_path}:/entrypoint.sh:ro',  # Mount entrypoint.sh
    ]

    # config_path points to the specific config folder (e.g., test-1-1)
    # scenario.osc and run_files are at the parent level
    parent_path = os.path.dirname(config_path)
    docker_cmd.extend(['-v', f'{os.path.join(parent_path, "scenario.osc")}:/config/scenario.osc:ro'])
    docker_cmd.extend(['-v', f'{os.path.join(config_path, "scenario.config")}:/config/scenario.config:ro'])
    for run_file in run_files:
        docker_cmd.extend(['-v', f'{os.path.join(parent_path, "_config", run_file)}:/config/{run_file}:ro'])
    for config_file in config_files:
        docker_cmd.extend(['-v', f'{os.path.join(config_path, config_file)}:/config/{config_file}:ro'])

    env_vars = get_execution_env_variables(run_num, config_name)
    for key, value in env_vars.items():
        docker_cmd.extend(['-e', f'{key}={value}'])
    
    # Add PRE_COMMAND and POST_COMMAND if specified
    if pre_command:
        docker_cmd.extend(['-e', f'PRE_COMMAND="{pre_command}"'])
    if post_command:
        docker_cmd.extend(['-e', f'POST_COMMAND="{post_command}"'])

    if shell:
        # Interactive shell mode
        docker_cmd.extend(['-it', image, '/bin/bash'])
        logger.info(f"Opening interactive shell in Docker container: {image}")
    else:
        # Normal execution mode
        docker_cmd.append(image)

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
COMMAND="/entrypoint.sh"

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
    echo "/entrypoint.sh"
    echo "--------------------------------------------------------"
else
    INTERACTIVE=""
fi

mkdir -p ${RESULTS_DIR}
"""


def generate_docker_run_script(runs, run_data, config_path_result, pre_command, post_command, docker_image, results_dir, output_script_path):
    """Generate a shell script to run Docker containers sequentially.

    Args:
        results_dir: Directory where results are stored
        output_script_path: Path where the script should be written
    """

    configs = []
    for run_number in range(runs):
        for config_entry in run_data["configs"]:
            configs.append((docker_image, os.path.abspath(os.path.join(
                config_path_result, config_entry["name"])), config_entry['name'], run_number, pre_command, post_command, config_entry.get("_config_files", [])))


    if not configs:
        raise ValueError("At least one config configuration is required")

    # Use the first config's image as the default
    default_image = configs[0][0]

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

    total_configs = len(configs)

    # Generate docker run commands for each config
    for idx, config_tuple in enumerate(configs, 1):
        image, config_path, config_name, run_num, pre_command, post_command, config_files = config_tuple
        result_config_path = os.path.join("${RESULTS_DIR}", config_name, "_config")
        test_path = os.path.join("${RESULTS_DIR}", config_name, str(run_num))
        run_files = run_data.get("_test_files", [])
        cmd_line = get_commandline(image, config_path, test_path, config_name, config_files, run_files, run_num, pre_command=pre_command, post_command=post_command)

        # copy config files to output directory only for the first run
        if run_num == 0:
            script += f'echo "Copying configuration files to {result_config_path}..."\n'
            script += f'mkdir -p "{result_config_path}"\n'
            script += f'cp -r "{config_path}/"* "{result_config_path}" 2>/dev/null || true\n'
            script += f'echo ""\n\n'

        # Add progress message
        script += f'\necho ""\n'
        script += f'echo "{"=" * 60}"\n'
        script += f'echo "{idx}/{total_configs} Executing config {config_name}, run {run_num}"\n'
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

        # Add docker run command for this config
        script += 'docker run $INTERACTIVE \\\n'
        script += f'    --name "$CONTAINER_NAME" \\\n'
        script += '    $NETWORK_MODE \\\n'
        script += '    $GUI_OPTIONS \\\n'
        script += "\n".join(docker_params)
        script += f'\n    "$DOCKER_IMAGE" \\\n    $COMMAND\n\n'

        # Check exit code after each run
        if idx < total_configs:
            # Not the last one - check and continue
            script += '# Check exit code\n'
            script += 'EXIT_CODE=$?\n'
            script += 'if [ $EXIT_CODE -ne 0 ]; then\n'
            script += f'    echo "Error: Config {idx}/{total_configs} ({config_name}) failed with exit code $EXIT_CODE"\n'
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
            script += f'    echo "All {total_configs} config(s) completed successfully!"\n'
            script += f'    echo "{"=" * 60}"\n'
            script += 'else\n'
            script += f'    echo "Error: Config {idx}/{total_configs} ({config_name}) failed with exit code $EXIT_CODE"\n'
            script += 'fi\n'
            script += 'cleanup\n'
            script += 'exit $EXIT_CODE\n'

    try:
        with open(output_script_path, 'w') as f:
            f.write(script)
        os.chmod(output_script_path, 0o755)  # Make the script executable
        logger.debug(f"Generated Docker run script: {output_script_path}")
    except Exception as e:  # pylint: disable=broad-except
        logger.error(f"Error writing Docker run script: {e}")
        raise
