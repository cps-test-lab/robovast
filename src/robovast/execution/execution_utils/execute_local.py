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

import fnmatch
import logging
import os
import sys
import tempfile

from robovast.common import (COMPAT_VERSION, generate_execution_yaml_script,
                             get_execution_env_variables, load_config,
                             normalize_secondary_containers,
                             prepare_campaign_configs)
from robovast.common.cli import get_project_config
from robovast.common.common import get_scenario_parameters
from robovast.common.config_generation import generate_scenario_variations
from robovast.common.execution import (build_job_parameter_documents,
                                       dump_multi_document_yaml,
                                       write_job_links_manifest)
from robovast.execution.packer import build_jobs

logger = logging.getLogger(__name__)


def initialize_local_execution(config, output_dir, runs, feedback_callback=logging.debug,
                               skip_resource_allocation=True, log_tree=False):
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
    campaign_data, _ = generate_scenario_variations(
        variation_file=config_path,
        progress_update_callback=None,
        output_dir=temp_dir.name
    )

    if not campaign_data["configs"]:
        logger.error("No configs found in vast-file")
        feedback_callback("Error: No configs found in vast-file.", file=sys.stderr)
        sys.exit(1)

    # Filter to configs matching the pattern if requested
    if config:
        matched = [cfg for cfg in campaign_data["configs"] if fnmatch.fnmatch(cfg['name'], config)]

        if not matched:
            feedback_callback(f"Error: No configs matched pattern '{config}'.", file=sys.stderr)
            feedback_callback("Available configs:")
            for cfg in campaign_data["configs"]:
                feedback_callback(f"  - {cfg['name']}")
            sys.exit(1)

        campaign_data["configs"] = matched

    logger.debug(f"Preparing {len(campaign_data['configs'])} configs from {config_path}...")
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
        prepare_campaign_configs(config_path_result, campaign_data)
        logger.debug(f"Config path: {config_path_result}")
    except Exception as e:  # pylint: disable=broad-except
        feedback_callback(f"Error preparing run configs: {e}", file=sys.stderr)
        sys.exit(1)

    logger.debug(f"Configuration files prepared in: {config_dir}")

    # Check if run_as_user differs from local user and warn about potential permission issues
    execution_params = campaign_data.get("execution", {})
    run_as_user = execution_params.get("run_as_user", 1000)
    host_uid = os.getuid()
    if run_as_user != host_uid:
        logger.warning(f"Container will run as UID {run_as_user}, but host user is UID {host_uid}. "
                       f"This may cause permission issues with bind-mounted directories. "
                       f"Consider setting 'run_as_user: {host_uid}' in your .vast config for local testing.")

    generate_compose_run_script(runs, campaign_data, config_path_result, pre_command, post_command,
                                docker_image, results_dir, os.path.join(config_dir, "run.sh"),
                                skip_resource_allocation=skip_resource_allocation,
                                log_tree=log_tree)
    return os.path.join(config_dir, "run.sh")


RUN_SCRIPT_HEADER = """#!/usr/bin/env bash

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)

# Default Docker image
DOCKER_IMAGE="ghcr.io/cps-test-lab/robovast:latest"
USE_GUI=true
START_ONLY=false
RUN_ID="CAMPAIGN_NAME_PLACEHOLDER-$(date +%Y-%m-%d-%H%M%S%N | cut -c1-19)"
RESULTS_DIR=

# Variables to track cleanup and interrupt state
CLEANUP_DONE=0
COMPOSE_PID=
LOG_PID=
SIGINT_COUNT=0
ABORT_ON_FAILURE=false
OVERALL_EXIT_CODE=0
SCENARIO_EXECUTION_PARAMS=""

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
    --abort-on-failure  Stop execution after the first failed run config
    --log-tree, -t      Pass --log-tree to scenario execution
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
        --log-tree | -t)
            SCENARIO_EXECUTION_PARAMS="-t"
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

# Pull image if not available locally
if ! docker image inspect "$DOCKER_IMAGE" > /dev/null 2>&1; then
    echo "Docker image '$DOCKER_IMAGE' not found locally. Downloading..."
    docker pull "$DOCKER_IMAGE"
    echo ""
fi

# Compatibility version check (reads /etc/robovast_compat_version inside the container)
IMAGE_COMPAT=$(docker run --rm "$DOCKER_IMAGE" cat /etc/robovast_compat_version 2>/dev/null || echo "")
if [ -z "$IMAGE_COMPAT" ] || [ "$IMAGE_COMPAT" != "@@COMPAT_VERSION@@" ]; then
    echo "ERROR: Compatibility version mismatch!"
    echo "  Host robovast expects compat version: @@COMPAT_VERSION@@"
    echo "  Container image provides: ${IMAGE_COMPAT:-<missing>}"
    echo "  Image: $DOCKER_IMAGE"
    echo ""
    echo "  Fix: Pull the latest image with 'docker pull $DOCKER_IMAGE'"
    echo "       or rebuild with the matching robovast version."
    exit 1
fi
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


def _build_packed_compose_yaml(
    docker_image,
    out_path,
    results_dir_var,
    job,
    param_file_rel,
    run_files,
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
    skip_resource_allocation=True,
    scenario_execution_params='',
    scenario_file_name='scenario.osc',
):
    """Build docker-compose YAML for one job.

    ``/out`` is the campaign root (scenario_execution writes per-config
    ``_output_dir`` subdirs), a single multi-document parameter file is mounted
    at ``/config/scenario.params.yaml``, and each config's generated files are
    mounted under ``/config/<config-name>/`` to avoid collisions. Used for both
    single-config (one config per job) and packed (several configs per job) runs.

    Secondary containers (e.g. a ``scenario_execution_server`` simulation server)
    are started once and span the whole job: the main ``scenario_execution``
    drives a per-config ``reset(params)`` over the ``/ipc`` socket between the
    job's configs. They receive the same packed param file and namespaced
    per-config file mounts as the main container so file-valued reset parameters
    resolve identically.
    """

    def quote(s):
        return s.replace('"', '\\"')

    has_secondaries = bool(secondary_containers)

    def _packed_config_mounts():
        """Volume mount lines shared by the main and secondary containers."""
        yield f'      - "{quote(results_dir_var)}/{param_file_rel}:/config/scenario.params.yaml:ro"'
        yield f'      - "{quote(results_dir_var)}/_config/{scenario_file_name}:/config/{scenario_file_name}:ro"'
        for run_file in run_files:
            yield f'      - "{quote(results_dir_var)}/_config/{run_file}:/config/{run_file}:ro"'
        # Per-config generated files, namespaced under /config/<config-name>/
        for config_data in (it.config for it in job.items):
            config_name = config_data.get("name", "")
            for deploy_rel, _src in config_data.get("_config_files", []):
                yield (
                    f'      - "{quote(results_dir_var)}/{config_name}/_config/{deploy_rel}'
                    f':/config/{config_name}/{deploy_rel}:ro"'
                )
        if has_secondaries:
            yield '      - shared_tmp:/tmp'
            yield '      - shared_ipc:/ipc'

    # Environment selecting the packed parameter file + per-scenario output.
    # /out is the campaign root; per-config results go to /out/<config>/<run> via
    # each document's _output_dir (SCENARIO_OUTPUT_DIR), while this job's job-level
    # artifacts (sysinfo, resource monitor, logs) go to a per-job subdir so they
    # don't collide across jobs.
    packed_env_lines = [
        "      - SCENARIO_PARAMETER_FILE=/config/scenario.params.yaml",
        "      - OUTPUT_RESULT_PER_SCENARIO=true",
        f"      - OUTPUT_DIR=/out/_jobs/job-{job.index}",
        "      - SCENARIO_OUTPUT_DIR=/out",
    ]

    lines = []
    lines.append("services:")
    lines.append("  robovast:")
    lines.append(f"    image: ${{DOCKER_IMAGE}}")
    lines.append(f"    container_name: robovast")
    lines.append(f"    init: true")
    if main_gpu:
        lines.append("    runtime: nvidia")
    if has_secondaries:
        lines.append("    ipc: shareable")

    lines.append("    volumes:")
    lines.append(f'      - "{quote(out_path)}:/out"')
    lines.append(f'      - "{quote(results_dir_var)}/_transient/entrypoint.sh:/config/entrypoint.sh:ro"')
    lines.append(f'      - "{quote(results_dir_var)}/_transient/collect_sysinfo.py:/config/collect_sysinfo.py:ro"')
    lines.append(f'      - "{quote(results_dir_var)}/_transient/monitor_resources.py:/config/monitor_resources.py:ro"')
    lines.extend(_packed_config_mounts())
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
    lines.append(f"      - SCENARIO_FILE={scenario_file_name}")
    lines.extend(packed_env_lines)
    if scenario_execution_params:
        lines.append(f"      - SCENARIO_EXECUTION_PARAMETERS={scenario_execution_params}")
    if use_gui_block:
        lines.append("      - DISPLAY=${DISPLAY:-:0}")
        lines.append("      - LIBGL_ALWAYS_SOFTWARE=${LIBGL_ALWAYS_SOFTWARE:-0}")
    if main_gpu:
        lines.append("      - QT_X11_NO_MITSHM=1")
        lines.append("      - NVIDIA_VISIBLE_DEVICES=all")
        lines.append("      - NVIDIA_DRIVER_CAPABILITIES=all")

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
        lines.append(f'      - "{quote(out_path)}:/out"')
        lines.append(f'      - "{quote(results_dir_var)}/_transient/secondary_entrypoint.sh:/config/secondary_entrypoint.sh:ro"')
        lines.append(f'      - "{quote(results_dir_var)}/_transient/collect_sysinfo.py:/config/collect_sysinfo.py:ro"')
        lines.append(f'      - "{quote(results_dir_var)}/_transient/monitor_resources.py:/config/monitor_resources.py:ro"')
        lines.extend(_packed_config_mounts())
        if use_gui_block:
            lines.append("      - /tmp/.X11-unix:/tmp/.X11-unix:rw")
            lines.append("      - /dev/dri:/dev/dri")
        lines.append("    environment:")
        lines.append(f"      - CONTAINER_NAME={sc_name}")
        lines.append(f"      - SCENARIO_FILE={scenario_file_name}")
        lines.extend(packed_env_lines)
        for key, value in env_vars.items():
            lines.append(f"      - {key}={value}")
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


def _emit_compose_step(compose_file, compose_yaml, idx, total, label, has_secondaries, noun,
                       post_down=""):
    """Return the shell text that writes, runs, waits on and tears down one compose stack.

    Shared by the single-config and packed (multi-config) code paths. ``idx`` is
    1-based; the final step (``idx == total``) emits the overall summary/exit.
    Each call corresponds to exactly one ``docker compose up``/``down`` cycle:
    every container in the stack starts once and stays up until the step
    completes (no per-parameter-set restarts).

    ``post_down`` is shell injected right after this step's ``docker compose
    down`` and before the failure/summary handling — so it runs for every step,
    including the last (whose summary block ends in ``exit``). The packed path
    uses it to create this job's artifact links per job (Ctrl+C-safe).
    """
    s = f'CURRENT_COMPOSE_FILE="{compose_file}"\n'
    s += 'export DOCKER_IMAGE RESULTS_DIR AVAILABLE_CPUS AVAILABLE_MEM LIBGL_ALWAYS_SOFTWARE ROBOVAST_COMMAND SECONDARY_COMMAND ROBOVAST_TTY ROBOVAST_STDIN_OPEN SCENARIO_EXECUTION_PARAMS\n'
    s += f'cat > "{compose_file}" << \'COMPOSE_EOF\'\n'
    s += compose_yaml + '\n'
    s += 'COMPOSE_EOF\n\n'

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
    compose_wait = 'COMPOSE_PID=$!\n'
    compose_wait += (
        'wait "$COMPOSE_PID" 2>/dev/null\n'
        'WAIT_CODE=$?\n'
        'while [ "$WAIT_CODE" -ge 128 ] && kill -0 "$COMPOSE_PID" 2>/dev/null; do\n'
        '    wait "$COMPOSE_PID" 2>/dev/null\n'
        '    WAIT_CODE=$?\n'
        'done\n'
        'COMPOSE_PID=\n'
    )
    compose_wait += (
        'EXIT_CODE=$WAIT_CODE\n'
        'if [ "$SIGINT_COUNT" -gt 0 ]; then\n'
        '    cleanup\n'
        '    exit 130\n'
        'fi\n'
    )
    if has_secondaries:
        s += compose_bg
        s += compose_wait
    else:
        s += f'if [ "$START_ONLY" = true ]; then\n'
        s += f'    docker compose -f "{compose_file}" run --rm --entrypoint /bin/bash robovast\n'
        s += '    EXIT_CODE=$?\n'
        s += f'else\n'
        for line in compose_bg.splitlines(keepends=True):
            s += f'    {line}'
        for line in compose_wait.splitlines(keepends=True):
            s += f'    {line}'
        s += f'fi\n'

    s += f'docker compose -f "{compose_file}" down --volumes --timeout 5 2>/dev/null || true\n'
    if post_down:
        s += post_down
    if idx < total:
        s += 'if [ $EXIT_CODE -ne 0 ]; then\n'
        s += f'    echo "Warning: {label} failed with exit code $EXIT_CODE"\n'
        s += '    OVERALL_EXIT_CODE=$EXIT_CODE\n'
        s += '    if [ "$ABORT_ON_FAILURE" = true ]; then\n'
        s += '        cleanup\n'
        s += '        exit $EXIT_CODE\n'
        s += '    fi\n'
        s += 'fi\n\n'
    else:
        s += 'if [ $EXIT_CODE -ne 0 ]; then\n'
        s += '    OVERALL_EXIT_CODE=$EXIT_CODE\n'
        s += 'fi\n'
        s += 'if [ $OVERALL_EXIT_CODE -eq 0 ]; then\n'
        s += f'    echo ""\n'
        s += f'    echo "{"=" * 60}"\n'
        s += f'    echo "All {total} {noun} completed successfully!"\n'
        s += f'    echo "{"=" * 60}"\n'
        s += 'else\n'
        s += f'    echo "Error: One or more of {total} {noun} failed (last exit code: $OVERALL_EXIT_CODE)"\n'
        s += 'fi\n'
        s += 'cleanup\n'
        s += 'exit $OVERALL_EXIT_CODE\n'
    return s


def generate_compose_run_script(runs, campaign_data, config_path_result, pre_command, post_command,
                                docker_image, results_dir, output_script_path,
                                skip_resource_allocation=False, log_tree=False):
    """Generate a shell script to run Docker Compose stacks sequentially.

    Args:
        runs: Number of runs per config
        campaign_data: Dictionary containing configs and run files
        config_path_result: Path to the config results directory
        pre_command: Command to run before execution (optional)
        post_command: Command to run after execution (optional)
        docker_image: Docker image to use
        results_dir: Directory where results are stored
        output_script_path: Path where the script should be written
    """
    run_files = campaign_data.get("_run_files", [])

    if not campaign_data["configs"]:
        raise ValueError("At least one config configuration is required")

    execution_params = campaign_data.get("execution", {})
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

    # Execution timeout (seconds) – None means no limit
    timeout = execution_params.get("timeout")

    # Secondary containers
    secondary_containers = execution_params.get("secondary_containers") or []
    normalized_secondary = normalize_secondary_containers(secondary_containers)

    script = RUN_SCRIPT_HEADER.replace(
        'DOCKER_IMAGE="ghcr.io/cps-test-lab/robovast:latest"',
        f'DOCKER_IMAGE="{docker_image}"', 1
    ).replace(
        'CAMPAIGN_NAME_PLACEHOLDER',
        (campaign_data.get('metadata') or {}).get('name', 'campaign'), 1
    ).replace(
        'RESULTS_DIR=',
        f'RESULTS_DIR="{results_dir}/${{RUN_ID}}"', 1
    ).replace(
        '@@COMPAT_VERSION@@', str(COMPAT_VERSION),
    )

    # Warn if timeout is configured (not respected in local runs)
    if timeout:
        script += f'echo "Warning: execution.timeout is set to {timeout}s but is not enforced during local runs."\n'
        script += f'echo ""\n'

    # Copy out_template to results dir
    script += f'echo "Copying out_template contents to ${{RESULTS_DIR}}..."\n'
    script += f'cp -r "${{SCRIPT_DIR}}/out_template/"* "${{RESULTS_DIR}}/"\n'
    script += f'echo ""\n\n'

    script += generate_execution_yaml_script(runs, execution_params=campaign_data.get("execution", {}))

    scenario_file_name = os.path.basename(campaign_data.get("scenario_file", "scenario.osc"))
    scenario_execution_params = "-t" if log_tree else "${SCENARIO_EXECUTION_PARAMS}"

    def _emit_preamble(banner, mkdir_dirs):
        """Per-step banner, output-dir creation, resource vars and command selection."""
        s = f'\necho ""\n'
        s += f'echo "{"=" * 60}"\n'
        s += f'echo "{banner}"\n'
        s += f'echo "{"=" * 60}"\n'
        s += f'echo ""\n\n'
        for d in mkdir_dirs:
            s += f'mkdir -p "{d}/logs"\n'
            s += f'chmod -R 777 "{d}"\n'
        # Set AVAILABLE_CPUS/MEM from configured resources
        s += f'AVAILABLE_CPUS="{main_cpu}"\n'
        if main_memory:
            s += f'AVAILABLE_MEM="{main_memory}"\n'
        else:
            s += "AVAILABLE_MEM=\"$(awk '/MemTotal/ {print $2 * 1024}' /proc/meminfo)\"\n"
        s += '\n# Determine command and interactive settings based on START_ONLY\n'
        s += 'if [ "$START_ONLY" = true ]; then\n'
        s += '    ROBOVAST_COMMAND="/bin/bash"\n'
        s += '    SECONDARY_COMMAND="/bin/bash"\n'
        s += '    ROBOVAST_TTY="true"\n'
        s += '    ROBOVAST_STDIN_OPEN="true"\n'
        s += 'else\n'
        s += '    # Use string format for command to be consistent with variable substitution\n'
        s += '    ROBOVAST_COMMAND="/bin/bash /config/entrypoint.sh"\n'
        s += '    SECONDARY_COMMAND="/bin/bash /config/secondary_entrypoint.sh"\n'
        s += '    ROBOVAST_TTY="false"\n'
        s += '    ROBOVAST_STDIN_OPEN="false"\n'
        s += 'fi\n\n'
        return s

    has_secondaries = bool(normalized_secondary)

    # Every run goes through the job mechanism: configs_per_job=1 yields one job
    # per (config, run), >1 packs several configs per job. Both produce the same
    # layout — results in <config>/<run>/ and job artifacts in _jobs/job-N/ with
    # a <config>/<run>/job symlink.
    scenario_path = os.path.join(
        os.path.dirname(campaign_data["vast"]), campaign_data["scenario_file"])
    scenario_name = next(iter(get_scenario_parameters(scenario_path).keys()))

    jobs = build_jobs(campaign_data["configs"], runs, execution_params)
    os.makedirs(os.path.join(config_path_result, "_transient"), exist_ok=True)
    # Canonical record of the per-job artifact links (also used by the cluster
    # share archiver). Local runs create the links inline per job below so a
    # Ctrl+C only loses the job active at cancel time.
    write_job_links_manifest(os.path.join(config_path_result, "_transient"), jobs)
    total = len(jobs)
    for idx, job in enumerate(jobs, 1):
        documents = build_job_parameter_documents(job, scenario_name)
        param_rel = f"_transient/job-{job.index}.params.yaml"
        with open(os.path.join(config_path_result, param_rel), 'w') as f:
            f.write(dump_multi_document_yaml(documents))

        compose_file = f"/tmp/robovast_compose_job-{job.index}.yml"
        mkdir_dirs = [
            os.path.join("${RESULTS_DIR}", it.config_name, str(it.run_number))
            for it in job.items
        ]
        names = ", ".join(job.config_names)
        script += _emit_preamble(
            f"{idx}/{total} Executing job {job.index} "
            f"({len(job.items)} parameter set(s): {names})", mkdir_dirs)
        env_vars = get_execution_env_variables(
            0, "", campaign_data.get('execution', {}).get('env'))
        compose_yaml = _build_packed_compose_yaml(
            docker_image=docker_image, out_path="${RESULTS_DIR}", results_dir_var="${RESULTS_DIR}",
            job=job, param_file_rel=param_rel, run_files=run_files, env_vars=env_vars,
            pre_command=pre_command, post_command=post_command, uid=uid, gid=gid,
            main_cpu=main_cpu, main_memory=main_memory, main_gpu=main_gpu,
            secondary_containers=normalized_secondary, use_gui_block=True,
            skip_resource_allocation=skip_resource_allocation,
            scenario_execution_params=scenario_execution_params,
            scenario_file_name=scenario_file_name,
        )
        # Create this job's artifact links right after it finishes (injected
        # after the compose `down`, before the step's summary/exit), so a
        # Ctrl+C only loses the links for the job active at cancel time.
        # Each <config>/<run>/job points at this job's _jobs/job-N dir.
        link_cmds = "".join(
            f'ln -sfn "../../_jobs/job-{job.index}" '
            f'"{os.path.join("${RESULTS_DIR}", it.config_name, str(it.run_number))}/job"\n'
            for it in job.items
        )
        script += _emit_compose_step(
            compose_file, compose_yaml, idx, total,
            f"Job {idx}/{total}", has_secondaries, "job(s)", post_down=link_cmds)

    try:
        with open(output_script_path, 'w') as f:
            f.write(script)
        os.chmod(output_script_path, 0o755)
        logger.debug(f"Generated Docker Compose run script: {output_script_path}")
    except Exception as e:  # pylint: disable=broad-except
        logger.error(f"Error writing Docker Compose run script: {e}")
        raise
