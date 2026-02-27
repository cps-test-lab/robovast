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

from robovast.common import (generate_execution_yaml_script,
                             get_execution_env_variables, load_config,
                             normalize_secondary_containers,
                             prepare_run_configs)
from robovast.common.cli import get_project_config
from robovast.common.config_generation import generate_scenario_variations

logger = logging.getLogger(__name__)



def initialize_local_execution(config, output_dir, runs, feedback_callback=logging.debug,
                               skip_resource_allocation=False):
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

    # Use execution_parameters value if runs is not provided
    if runs is None:
        if "runs" not in execution_parameters:
            logger.error("Number of runs not specified in command or config")
            feedback_callback("Error: Number of runs not specified in command or config.")
            sys.exit(1)
        else:
            runs = execution_parameters["runs"]

    logger.debug(f"Using Docker image: {docker_image}")

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
        config_path_result = os.path.join(config_dir, "out_template")
        prepare_run_configs(config_path_result, run_data)
        logger.debug(f"Config path: {config_path_result}")
    except Exception as e:  # pylint: disable=broad-except
        feedback_callback(f"Error preparing run configs: {e}", file=sys.stderr)
        sys.exit(1)

    logger.debug(f"Configuration files prepared in: {config_dir}")

    # Check if run_as_user differs from local user and warn about potential permission issues
    execution_params = run_data.get("execution", {})
    run_as_user = execution_params.get("run_as_user", 1000)
    host_uid = os.getuid()
    if run_as_user != host_uid:
        logger.warning(f"Container will run as UID {run_as_user}, but host user is UID {host_uid}. "
                       f"This may cause permission issues with bind-mounted directories. "
                       f"Consider setting 'run_as_user: {host_uid}' in your .vast config for local testing.")

    generate_compose_run_script(runs, run_data, config_path_result, pre_command, post_command,
                                docker_image, results_dir, os.path.join(config_dir, "run.sh"),
                                skip_resource_allocation=skip_resource_allocation)
    return os.path.join(config_dir, "run.sh")


RUN_SCRIPT_HEADER = """#!/usr/bin/env bash

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)

# Default Docker image
DOCKER_IMAGE="ghcr.io/cps-test-lab/robovast:latest"
USE_GUI=true
START_ONLY=false
RUN_ID="run-$(date +%Y-%m-%d-%H%M%S)"
RESULTS_DIR=

# Variables to track cleanup and interrupt state
CLEANUP_DONE=0
COMPOSE_PID=
LOG_PID=
SIGINT_COUNT=0
ABORT_ON_FAILURE=false
OVERALL_EXIT_CODE=0

# Cleanup function
cleanup() {
    if [ $CLEANUP_DONE -eq 1 ]; then
        return
    fi
    CLEANUP_DONE=1

    if [ -n "$LOG_PID" ]; then
        kill "$LOG_PID" 2>/dev/null || true
        LOG_PID=
    fi

    echo ""
    echo "Cleaning up containers..."
    if [ -n "$CURRENT_COMPOSE_FILE" ]; then
        docker compose -f "$CURRENT_COMPOSE_FILE" down --volumes --timeout 5 2>/dev/null || true
    fi
}

# SIGINT handler: first press triggers graceful shutdown; subsequent presses force-kill
handle_sigint() {
    SIGINT_COUNT=$((SIGINT_COUNT + 1))
    if [ $SIGINT_COUNT -eq 1 ]; then
        echo ""
        echo "Stopping... (press Ctrl+C again to force exit)"
        if [ -n "$COMPOSE_PID" ]; then
            kill -TERM "$COMPOSE_PID" 2>/dev/null || true
        fi
        # Keep streaming logs while containers shut down
        if [ -n "$CURRENT_COMPOSE_FILE" ]; then
            docker compose -f "$CURRENT_COMPOSE_FILE" logs --follow 2>/dev/null &
            LOG_PID=$!
        fi
    else
        echo ""
        echo "Force exiting..."
        if [ -n "$LOG_PID" ]; then
            kill "$LOG_PID" 2>/dev/null || true
            LOG_PID=
        fi
        if [ -n "$COMPOSE_PID" ]; then
            disown "$COMPOSE_PID" 2>/dev/null || true
            kill -KILL "$COMPOSE_PID" 2>/dev/null || true
        fi
        if [ -n "$CURRENT_COMPOSE_FILE" ]; then
            docker compose -f "$CURRENT_COMPOSE_FILE" kill 2>/dev/null || true
        fi
        cleanup
        exit 130
    fi
}

# Set up signal handlers
trap 'handle_sigint' SIGINT
trap 'cleanup; exit 130' SIGTERM

# Show help
show_help() {
    cat << EOF
Usage: $(basename "$0") [OPTIONS]

Run the robovast Docker containers.

OPTIONS:
    --image IMAGE       Use a custom Docker image (default: ghcr.io/cps-test-lab/robovast:latest)
    --no-gui            Disable host GUI support
    --results-dir DIR   Override the results output directory
    --start-only        Start the robovast container with a shell, skipping the entrypoint script
    --abort-on-failure  Stop execution after the first failed test config
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
        --no-gui)
            USE_GUI=false
            shift
            ;;
        --start-only)
            START_ONLY=true
            shift
            ;;
        --abort-on-failure)
            ABORT_ON_FAILURE=true
            shift
            ;;
        --results-dir)
            if [[ "$2" != /* ]]; then
                echo "Error: --results-dir must be an absolute path (starting with /)"
                exit 1
            fi
            echo "Overriding results directory to: $2"
            RESULTS_DIR="$2/${RUN_ID}"
            shift 2
            ;;
        *)
            break
            ;;
    esac
done

# GUI setup
GUI_DEVICES=""
GUI_ENV=""
HAS_DRI=false
if [ "$USE_GUI" = true ]; then
    xhost +local: > /dev/null 2>&1
    GUI_ENV="DISPLAY=${DISPLAY}"
    GUI_DEVICES="/tmp/.X11-unix:/tmp/.X11-unix:rw"
    if [ -e /dev/dri ]; then
        HAS_DRI=true
        export LIBGL_ALWAYS_SOFTWARE="${LIBGL_ALWAYS_SOFTWARE:-0}"
    else
        export LIBGL_ALWAYS_SOFTWARE=1
    fi
fi

mkdir -p "${RESULTS_DIR}"
"""


def _compose_resources_block(cpu, memory, indent="    "):
    """Return deploy.resources.limits YAML lines for a service, or empty string if none specified."""
    if not cpu and not memory:
        return ""
    lines = [
        f"{indent}deploy:",
        f"{indent}  resources:",
        f"{indent}    limits:",
    ]
    if cpu:
        lines.append(f"{indent}      cpus: '{cpu}'")
    if memory:
        lines.append(f"{indent}      memory: {memory}")
    return "\n".join(lines)


def _build_compose_yaml(
    docker_image,
    test_path,
    script_dir_var,
    results_dir_var,
    config_name,
    run_num,
    run_files,
    config_files,
    env_vars,
    pre_command,
    post_command,
    uid,
    gid,
    main_cpu,
    main_memory,
    main_gpu,
    secondary_containers,
    use_gui_block,
    skip_resource_allocation=False,
):
    """Build the docker-compose YAML content for one test run as a shell heredoc string."""

    def quote(s):
        return s.replace('"', '\\"')

    has_secondaries = bool(secondary_containers)

    def _config_volume_mounts():
        """Yield volume mount lines shared by main and secondary containers."""
        yield f'      - "{quote(results_dir_var)}/scenario.osc:/config/scenario.osc:ro"'
        yield f'      - "{quote(results_dir_var)}/{config_name}/scenario.config:/config/scenario.config:ro"'
        for run_file in run_files:
            yield f'      - "{quote(results_dir_var)}/_config/{run_file}:/config/{run_file}:ro"'
        for config_file in config_files:
            yield f'      - "{quote(results_dir_var)}/{config_name}/_config/{config_file[0]}:/config/{config_file[0]}:ro"'
        if has_secondaries:
            yield '      - shared_tmp:/tmp'
            yield '      - shared_ipc:/ipc'

    lines = []
    lines.append("services:")
    lines.append("  robovast:")
    lines.append(f"    image: ${{DOCKER_IMAGE}}")
    lines.append(f"    container_name: robovast")
    if main_gpu:
        lines.append("    runtime: nvidia")

    if has_secondaries:
        lines.append("    ipc: shareable")

    lines.append("    volumes:")
    lines.append(f'      - "{quote(test_path)}:/out"')
    lines.append(f'      - "{quote(script_dir_var)}/out_template/entrypoint.sh:/config/entrypoint.sh:ro"')
    lines.append(f'      - "{quote(script_dir_var)}/out_template/collect_sysinfo.py:/config/collect_sysinfo.py:ro"')
    lines.extend(_config_volume_mounts())
    if use_gui_block:
        lines.append("      - /tmp/.X11-unix:/tmp/.X11-unix:rw")
        lines.append("      - /dev/dri:/dev/dri")

    lines.append("    environment:")
    for key, value in env_vars.items():
        lines.append(f"      - {key}={value}")
    if pre_command:
        lines.append(f'      - PRE_COMMAND={pre_command}')
    if post_command:
        lines.append(f'      - POST_COMMAND={post_command}')
    lines.append("      - AVAILABLE_CPUS=${AVAILABLE_CPUS}")
    lines.append("      - AVAILABLE_MEM=${AVAILABLE_MEM}")
    if use_gui_block:
        lines.append("      - DISPLAY=${DISPLAY:-:0}")
        lines.append("      - LIBGL_ALWAYS_SOFTWARE=${LIBGL_ALWAYS_SOFTWARE:-0}")
    if main_gpu:
        lines.append("      - QT_X11_NO_MITSHM=1")
        lines.append("      - NVIDIA_VISIBLE_DEVICES=all")
        lines.append("      - NVIDIA_DRIVER_CAPABILITIES=all")

    # Resource limits for main container
    if not skip_resource_allocation:
        res = _compose_resources_block(main_cpu, main_memory)
        if res:
            lines.append(res)

    lines.append(f"    user: \"{uid}:{gid}\"")
    lines.append("    stop_grace_period: 60s")
    lines.append("    command: ${ROBOVAST_COMMAND}")
    lines.append("    tty: ${ROBOVAST_TTY}")
    lines.append("    stdin_open: ${ROBOVAST_STDIN_OPEN}")

    for sc in secondary_containers:
        sc_name = sc['name']
        sc_cpu = sc['resources'].get('cpu')
        sc_memory = sc['resources'].get('memory')
        sc_gpu = sc['resources'].get('gpu')

        lines.append(f"  {sc_name}:")
        lines.append(f"    image: ${{DOCKER_IMAGE}}")
        lines.append(f"    container_name: {sc_name}")
        if sc_gpu:
            lines.append("    runtime: nvidia")
        lines.append(f"    network_mode: service:robovast")
        lines.append(f"    ipc: service:robovast")
        lines.append(f"    depends_on:")
        lines.append(f"      - robovast")
        lines.append("    volumes:")
        lines.append(f'      - "{quote(test_path)}:/out"')
        lines.append(f'      - "{quote(script_dir_var)}/out_template/secondary_entrypoint.sh:/config/secondary_entrypoint.sh:ro"')
        lines.append(f'      - "{quote(script_dir_var)}/out_template/collect_sysinfo.py:/config/collect_sysinfo.py:ro"')
        lines.extend(_config_volume_mounts())
        if use_gui_block:
            lines.append("      - /tmp/.X11-unix:/tmp/.X11-unix:rw")
            lines.append("      - /dev/dri:/dev/dri")
        lines.append("    environment:")
        lines.append(f"      - CONTAINER_NAME={sc_name}")
        lines.append("      - ROS_LOG_DIR=/out/logs")
        if use_gui_block:
            lines.append("      - DISPLAY=${DISPLAY:-:0}")
            lines.append("      - LIBGL_ALWAYS_SOFTWARE=${LIBGL_ALWAYS_SOFTWARE:-0}")
        if sc_gpu:
            lines.append("      - QT_X11_NO_MITSHM=1")
            lines.append("      - NVIDIA_VISIBLE_DEVICES=all")
            lines.append("      - NVIDIA_DRIVER_CAPABILITIES=all")

        if not skip_resource_allocation:
            sc_res = _compose_resources_block(sc_cpu, sc_memory)
            if sc_res:
                lines.append(sc_res)

        lines.append(f"    user: \"{uid}:{gid}\"")
        # ROS2 nodes respond to SIGINT for graceful shutdown; docker compose
        # sends SIGTERM by default, causing non-clean exits and corrupted exit codes
        lines.append("    stop_signal: SIGINT")
        lines.append("    stop_grace_period: 5s")
        lines.append("    command: ${SECONDARY_COMMAND}")
        lines.append("    tty: ${ROBOVAST_TTY}")
        lines.append("    stdin_open: ${ROBOVAST_STDIN_OPEN}")

    if has_secondaries:
        lines.append("")
        lines.append("volumes:")
        lines.append("  shared_tmp:")
        lines.append("  shared_ipc:")
        lines.append("    driver: local")
        lines.append("    driver_opts:")
        lines.append("      type: tmpfs")
        lines.append("      device: tmpfs")
        lines.append('      o: "mode=0777"')

    return "\n".join(lines)


def generate_compose_run_script(runs, run_data, config_path_result, pre_command, post_command,
                                docker_image, results_dir, output_script_path,
                                skip_resource_allocation=False):
    """Generate a shell script to run Docker Compose stacks sequentially.

    Args:
        runs: Number of runs per config
        run_data: Dictionary containing configs and test files
        config_path_result: Path to the config results directory
        pre_command: Command to run before execution (optional)
        post_command: Command to run after execution (optional)
        docker_image: Docker image to use
        results_dir: Directory where results are stored
        output_script_path: Path where the script should be written
    """
    run_files = run_data.get("_test_files", [])
    execution_tasks = []

    for run_number in range(runs):
        for config_entry in run_data["configs"]:
            execution_tasks.append({
                'config_name': config_entry['name'],
                'config_path': os.path.abspath(os.path.join(config_path_result, config_entry["name"])),
                'config_files': config_entry.get("_config_files", []),
                'run_number': run_number,
            })

    if not execution_tasks:
        raise ValueError("At least one config configuration is required")

    execution_params = run_data.get("execution", {})
    run_as_user = execution_params.get("run_as_user")
    if run_as_user is None:
        run_as_user = os.getuid()
    uid = run_as_user
    gid = run_as_user

    # Resources for main container
    resources = execution_params.get("resources") or {}
    main_cpu = resources.get("cpu")
    main_memory = resources.get("memory")
    main_gpu = resources.get("gpu")

    # Secondary containers
    secondary_containers = execution_params.get("secondary_containers") or []
    normalized_secondary = normalize_secondary_containers(secondary_containers)

    script = RUN_SCRIPT_HEADER.replace(
        'DOCKER_IMAGE="ghcr.io/cps-test-lab/robovast:latest"',
        f'DOCKER_IMAGE="{docker_image}"', 1
    ).replace(
        'RESULTS_DIR=',
        f'RESULTS_DIR="{results_dir}/${{RUN_ID}}"', 1
    )

    # Copy out_template to results dir
    script += f'echo "Copying out_template contents to ${{RESULTS_DIR}}..."\n'
    script += f'cp -r "${{SCRIPT_DIR}}/out_template/"* "${{RESULTS_DIR}}/"\n'
    script += f'echo ""\n\n'

    script += generate_execution_yaml_script(runs, execution_params=run_data.get("execution", {}))

    for idx, task in enumerate(execution_tasks, 1):
        config_name = task['config_name']
        run_num = task['run_number']
        config_files = task['config_files']

        test_path = os.path.join("${RESULTS_DIR}", config_name, str(run_num))
        compose_file = f"/tmp/robovast_compose_{config_name}_{run_num}.yml"

        script += f'\necho ""\n'
        script += f'echo "{"=" * 60}"\n'
        script += f'echo "{idx}/{len(execution_tasks)} Executing config {config_name}, run {run_num}"\n'
        script += f'echo "{"=" * 60}"\n'
        script += f'echo ""\n\n'
        script += f'mkdir -p "{test_path}/logs"\n'
        script += f'chmod -R 777 "{test_path}"\n'

        # Set AVAILABLE_CPUS/MEM from configured resources
        script += f'AVAILABLE_CPUS="{main_cpu}"\n'
        if main_memory:
            script += f'AVAILABLE_MEM="{main_memory}"\n'
        else:
            script += "AVAILABLE_MEM=\"$(awk '/MemTotal/ {print $2 * 1024}' /proc/meminfo)\"\n"

        script += '\n# Determine command and interactive settings based on START_ONLY\n'
        script += 'if [ "$START_ONLY" = true ]; then\n'
        script += '    ROBOVAST_COMMAND="/bin/bash"\n'
        script += '    SECONDARY_COMMAND="/bin/bash"\n'
        script += '    ROBOVAST_TTY="true"\n'
        script += '    ROBOVAST_STDIN_OPEN="true"\n'
        script += 'else\n'
        script += '    # Use string format for command to be consistent with variable substitution\n'
        script += '    ROBOVAST_COMMAND="/bin/bash /config/entrypoint.sh"\n'
        script += '    SECONDARY_COMMAND="/bin/bash /config/secondary_entrypoint.sh"\n'
        script += '    ROBOVAST_TTY="false"\n'
        script += '    ROBOVAST_STDIN_OPEN="false"\n'
        script += 'fi\n\n'

        env_vars = get_execution_env_variables(run_num, config_name, run_data.get('execution', {}).get('env'))

        compose_yaml = _build_compose_yaml(
            docker_image=docker_image,
            test_path=test_path,
            script_dir_var="${SCRIPT_DIR}",
            results_dir_var="${RESULTS_DIR}",
            config_name=config_name,
            run_num=run_num,
            run_files=run_files,
            config_files=config_files,
            env_vars=env_vars,
            pre_command=pre_command,
            post_command=post_command,
            uid=uid,
            gid=gid,
            main_cpu=main_cpu,
            main_memory=main_memory,
            main_gpu=main_gpu,
            secondary_containers=normalized_secondary,
            use_gui_block=True,
            skip_resource_allocation=skip_resource_allocation,
        )

        script += f'CURRENT_COMPOSE_FILE="{compose_file}"\n'
        script += 'export DOCKER_IMAGE RESULTS_DIR SCRIPT_DIR AVAILABLE_CPUS AVAILABLE_MEM LIBGL_ALWAYS_SOFTWARE ROBOVAST_COMMAND SECONDARY_COMMAND ROBOVAST_TTY ROBOVAST_STDIN_OPEN\n'
        script += f'cat > "{compose_file}" << \'COMPOSE_EOF\'\n'
        script += compose_yaml + '\n'
        script += 'COMPOSE_EOF\n\n'

        # Run compose in background with SIGINT ignored in the child before exec.
        # Go programs (docker compose) preserve SIG_IGN across exec, so Ctrl+C
        # from the terminal does not reach docker compose directly. The compose
        # process stays in the same session so it keeps its controlling terminal,
        # which is required for proper container stop output and graceful shutdown.
        # Explicit signals (SIGTERM/SIGKILL) are sent by handle_sigint as needed.
        compose_bg = (
            f'( trap \'\' SIGINT; export COMPOSE_MENU=false;'
            f' docker compose -f "{compose_file}" up'
            f' --abort-on-container-exit'
            f' --exit-code-from robovast'
            f' 2> >(grep -v "Aborting on container exit" >&2)'
            f') &\n'
        )
        compose_wait = (
            'COMPOSE_PID=$!\n'
            'wait "$COMPOSE_PID" 2>/dev/null\n'
            'WAIT_CODE=$?\n'
            'while [ "$WAIT_CODE" -ge 128 ] && kill -0 "$COMPOSE_PID" 2>/dev/null; do\n'
            '    wait "$COMPOSE_PID" 2>/dev/null\n'
            '    WAIT_CODE=$?\n'
            'done\n'
            'COMPOSE_PID=\n'
            'EXIT_CODE=$WAIT_CODE\n'
            'if [ "$SIGINT_COUNT" -gt 0 ]; then\n'
            '    cleanup\n'
            '    exit 130\n'
            'fi\n'
        )
        if normalized_secondary:
            script += compose_bg
            script += compose_wait
        else:
            script += f'if [ "$START_ONLY" = true ]; then\n'
            script += f'    docker compose -f "{compose_file}" run --rm --entrypoint /bin/bash robovast\n'
            script += '    EXIT_CODE=$?\n'
            script += f'else\n'
            # Indent the wait-loop lines for readability inside the else block
            for line in compose_bg.splitlines(keepends=True):
                script += f'    {line}'
            for line in compose_wait.splitlines(keepends=True):
                script += f'    {line}'
            script += f'fi\n'

        if idx < len(execution_tasks):
            script += f'docker compose -f "{compose_file}" down --volumes --timeout 5 2>/dev/null || true\n'
            script += 'if [ $EXIT_CODE -ne 0 ]; then\n'
            script += f'    echo "Warning: Config {idx}/{len(execution_tasks)} ({config_name}) failed with exit code $EXIT_CODE"\n'
            script += '    OVERALL_EXIT_CODE=$EXIT_CODE\n'
            script += '    if [ "$ABORT_ON_FAILURE" = true ]; then\n'
            script += '        cleanup\n'
            script += '        exit $EXIT_CODE\n'
            script += '    fi\n'
            script += 'fi\n\n'
        else:
            script += f'docker compose -f "{compose_file}" down --volumes --timeout 5 2>/dev/null || true\n'
            script += 'if [ $EXIT_CODE -ne 0 ]; then\n'
            script += '    OVERALL_EXIT_CODE=$EXIT_CODE\n'
            script += 'fi\n'
            script += 'if [ $OVERALL_EXIT_CODE -eq 0 ]; then\n'
            script += f'    echo ""\n'
            script += f'    echo "{"=" * 60}"\n'
            script += f'    echo "All {len(execution_tasks)} config(s) completed successfully!"\n'
            script += f'    echo "{"=" * 60}"\n'
            script += 'else\n'
            script += f'    echo "Error: One or more of {len(execution_tasks)} config(s) failed (last exit code: $OVERALL_EXIT_CODE)"\n'
            script += 'fi\n'
            script += 'cleanup\n'
            script += 'exit $OVERALL_EXIT_CODE\n'

    try:
        with open(output_script_path, 'w') as f:
            f.write(script)
        os.chmod(output_script_path, 0o755)
        logger.debug(f"Generated Docker Compose run script: {output_script_path}")
    except Exception as e:  # pylint: disable=broad-except
        logger.error(f"Error writing Docker Compose run script: {e}")
        raise
